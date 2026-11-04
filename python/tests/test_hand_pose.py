"""Hand publication is model-neutral and self-describing on the wire."""
from visio_schema import make_channel, message_class
from visio_schema.v1.sensor.hand_pose_pb2 import HandPoseFrame


def test_hand_pose_channel_resolves_and_roundtrips_capture_time():
    channel = make_channel("/ego/camera/1/hand_pose", HandPoseFrame.DESCRIPTOR.full_name,
                           stream_id=20)
    assert channel.schema
    cls = message_class(channel.schema_name)
    frame = cls(frame_id="cam1", model="release-identity")
    frame.timestamp.seconds = 123
    frame.timestamp.nanos = 456
    restored = cls.FromString(frame.SerializeToString())
    assert restored.timestamp == frame.timestamp
    assert not restored.hands  # explicit clear is a valid message
