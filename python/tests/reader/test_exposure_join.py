"""The streaming exposure join must bound unresolved images and metadata gaps."""

import numpy as np
import pytest

from visio_schema import message_class
from visio_schema.reader import Frame, ImuSample, Record
from visio_schema.reader.exposure import join_exposure

CAMERA = "/ego/camera/0"
IMU = "/ego/imu/0/raw"
INFO = "visio_schema.v1.sensor.CameraFrameInfo"
T0 = 1_700_000_000_000_000_000


def frame(t_ns):
    return Frame(CAMERA, t_ns, np.zeros((8, 8), dtype=np.uint8))


def info(t_ns):
    msg = message_class(INFO)(
        exposure_us=4000, exposure_mid_offset_us=-2000, gain=2.0
    )
    msg.timestamp.FromNanoseconds(t_ns)
    return Record(CAMERA + "/frame_info", t_ns, INFO, msg)


def test_join_preserves_interleaved_sensor_order_until_exact_metadata_arrives():
    first, second = frame(T0), frame(T0 + 10_000_000)
    imu0 = ImuSample(IMU, T0, np.zeros(3), np.zeros(3))
    imu1 = ImuSample(IMU, T0 + 15_000_000, np.zeros(3), np.zeros(3))
    stream = [first, imu0, info(T0), second, info(second.t_ns), imu1]
    output = list(join_exposure(iter(stream), (CAMERA,)))
    assert [(e.topic, e.t_ns) for e in output] == [
        (e.topic, e.t_ns) for e in (first, imu0, second, imu1)
    ]
    assert output[1] is imu0 and output[3] is imu1
    for e in (output[0], output[2]):
        assert e.exposure.interpolated is False
        assert e.exposure.mid_ns == e.t_ns - 2_000_000


def test_join_rejects_an_unresolved_metadata_gap_at_the_stream_watermark():
    later = ImuSample(IMU, T0 + 100_000_001, np.zeros(3), np.zeros(3))
    with pytest.raises(ValueError, match="unresolved frame_info gap"):
        list(join_exposure(iter([frame(T0), later]), (CAMERA,),
                           max_gap_ns=100_000_000))


def test_join_rejects_interpolation_across_a_long_metadata_gap():
    stream = [info(T0), frame(T0 + 50_000_000), info(T0 + 200_000_000)]
    with pytest.raises(ValueError, match="interpolation gap exceeds limit"):
        list(join_exposure(iter(stream), (CAMERA,), max_gap_ns=100_000_000))


def test_join_refuses_an_image_that_exceeds_the_pending_byte_budget():
    image = frame(T0)
    with pytest.raises(ValueError, match="pending image byte limit"):
        list(join_exposure(iter([image, info(T0)]), (CAMERA,),
                           max_frame_bytes=image.image.nbytes - 1))
