"""scene_derivers.py — device-specific SceneUpdate derivers for visio-display.

Two derivers that synthesize a `foxglove.SceneUpdate` "viz twin" from a device's
own data, so Foxglove / an MCAP shows a hand shape without any downstream code:

- ``TactileSceneDeriver`` — JQ glove: join a ``foxglove.RawImage`` on
  ``/glove_*/tactile/*`` with its ``TactileLayout`` on ``.../layout`` and emit a
  bar-per-taxel scene on ``.../scene`` (height + blue->red = pressure).
- ``HandSkeletonDeriver`` — Quest hand tracking: turn a 26-joint
  ``foxglove.FrameTransforms`` on ``/quest/skeleton/hand_*`` into a sphere-per-joint
  + bone LINE_LIST scene on ``.../scene`` (left cyan, right amber).

Each mirrors :class:`TfDeriver`'s shape: ``derive(msg, ch) -> (Message, Channel) |
None``, sink-agnostic (the derived message rides the same ``sink.write`` path). The
hand shape / joint tables are device-specific — kept here in ``display`` (viewer
policy), not in the generic routing layer.
"""
from __future__ import annotations

from visio_schema import Message, make_channel
from visio_schema.display import hand_geometry as hg
from visio_schema.foxglove.Color_pb2 import Color
from visio_schema.foxglove.CubePrimitive_pb2 import CubePrimitive
from visio_schema.foxglove.FrameTransforms_pb2 import FrameTransforms
from visio_schema.foxglove.LinePrimitive_pb2 import LinePrimitive
from visio_schema.foxglove.Point3_pb2 import Point3
from visio_schema.foxglove.Pose_pb2 import Pose
from visio_schema.foxglove.Quaternion_pb2 import Quaternion
from visio_schema.foxglove.RawImage_pb2 import RawImage
from visio_schema.foxglove.SceneEntity_pb2 import SceneEntity
from visio_schema.foxglove.SceneUpdate_pb2 import SceneUpdate
from visio_schema.foxglove.SpherePrimitive_pb2 import SpherePrimitive
from visio_schema.foxglove.TextPrimitive_pb2 import TextPrimitive
from visio_schema.foxglove.Vector3_pb2 import Vector3
from visio_schema.v1.calibration.tactile_pb2 import TactileLayout

_LAYOUT_SCHEMA = "visio_schema.v1.calibration.TactileLayout"
_IMAGE_SCHEMA = "foxglove.RawImage"
_TF_SCHEMA = "foxglove.FrameTransforms"
_SCENE_SCHEMA = "foxglove.SceneUpdate"
# Synthetic stream ids for derived scene channels — high, to clear device ids.
_TACTILE_SCENE_BASE = 30000
_HAND_SCENE_BASE = 30100
_UNIT_Q = Quaternion(x=0, y=0, z=0, w=1)
# Building a scene (137 cubes / 26 spheres + serialize) costs a few ms — far
# slower than the ~288 fps tactile stream. Deriving one per frame backs up the
# bridge's read loop and stalls EVERY topic (raw data included). A scene at ~30 fps
# is plenty for viewing, so throttle by capture time; the data itself still passes
# through at full rate (the deriver only adds the .../scene twin).
_SCENE_MIN_PERIOD_NS = 33_000_000  # ~30 fps


def _ns(ts):
    return ts.seconds * 1_000_000_000 + ts.nanos


# --------------------------------------------------------------------------- #
# JQ glove: tactile RawImage + TactileLayout -> hand-shaped SceneUpdate        #
# --------------------------------------------------------------------------- #
class TactileSceneDeriver:
    """Join a glove's RawImage with its TactileLayout and emit a hand-shaped
    SceneUpdate on <data-topic>/scene (a cube per taxel: height + blue->red =
    pressure, labelled with the vendor taxel id)."""

    def __init__(self):
        self._placement = {}   # data_topic -> [(index, taxel_id, x, y)]
        self._scene_ch = {}    # data_topic -> derived scene Channel
        self._last_ns = {}     # data_topic -> capture ns of the last emitted scene
        self._next_stream = _TACTILE_SCENE_BASE

    def derive(self, msg, ch):
        topic = ch.topic
        if ch.schema_name == _LAYOUT_SCHEMA and topic.endswith("/layout"):
            lay = TactileLayout()
            lay.ParseFromString(msg.payload)
            data_topic = topic[: -len("/layout")]
            self._placement[data_topic] = hg.build_placement(lay, hg.side_is_left(data_topic))
            return None
        if ch.schema_name == _IMAGE_SCHEMA and "/tactile/" in topic:
            pl = self._placement.get(topic)
            if pl is None:
                return None                    # no layout seen yet
            ns = _ns(msg.timestamp)
            if ns - self._last_ns.get(topic, 0) < _SCENE_MIN_PERIOD_NS:
                return None                    # throttle scene to ~30 fps
            self._last_ns[topic] = ns
            out_ch = self._scene_ch.get(topic)
            if out_ch is None:
                out_ch = make_channel(topic + "/scene", _SCENE_SCHEMA, stream_id=self._next_stream)
                self._next_stream += 1
                self._scene_ch[topic] = out_ch
            img = RawImage()
            img.ParseFromString(msg.payload)
            frame = topic.strip("/").split("/")[0]  # /glove_left/tactile/0 -> glove_left
            scene = _tactile_scene(img.data, pl, frame, msg.timestamp)
            out = Message(stream_id=out_ch.id, payload=scene.SerializeToString())
            out.timestamp.CopyFrom(msg.timestamp)
            return out, out_ch
        return None


def _tactile_scene(data, placement, frame_id, timestamp):
    cubes, texts = [], []
    for index, taxel_id, x, y in placement:
        p = min(data[index] / hg.FULL_SCALE, 1.0) if index < len(data) else 0.0
        h = 0.004 + p * hg.MAX_H
        cubes.append(
            CubePrimitive(
                pose=Pose(position=Vector3(x=x, y=y, z=h / 2), orientation=_UNIT_Q),
                size=Vector3(x=hg.CUBE, y=hg.CUBE, z=h),
                color=Color(r=p, g=0.1, b=1.0 - p, a=1.0),
            )
        )
        texts.append(
            TextPrimitive(
                pose=Pose(position=Vector3(x=x, y=y, z=h + 0.004), orientation=_UNIT_Q),
                billboard=True, font_size=9.0, scale_invariant=True,
                color=Color(r=1.0, g=1.0, b=1.0, a=0.9), text=str(taxel_id),
            )
        )
    ent = SceneEntity(timestamp=timestamp, frame_id=frame_id, id="hand",
                      cubes=cubes, texts=texts)
    return SceneUpdate(entities=[ent])


# --------------------------------------------------------------------------- #
# Quest hand skeleton: FrameTransforms -> spheres + bones SceneUpdate          #
# --------------------------------------------------------------------------- #
# XrHandJointEXT ordinal -> child_frame_id, ported from visio-quest VisioPublisher.
_LH_FRAMES = [
    "lh_palm", "lh_wrist",
    "lh_thumb_meta", "lh_thumb_prox", "lh_thumb_dist", "lh_thumb_tip",
    "lh_index_meta", "lh_index_prox", "lh_index_int", "lh_index_dist", "lh_index_tip",
    "lh_middle_meta", "lh_middle_prox", "lh_middle_int", "lh_middle_dist", "lh_middle_tip",
    "lh_ring_meta", "lh_ring_prox", "lh_ring_int", "lh_ring_dist", "lh_ring_tip",
    "lh_little_meta", "lh_little_prox", "lh_little_int", "lh_little_dist", "lh_little_tip",
]
_RH_FRAMES = [n.replace("lh_", "rh_", 1) for n in _LH_FRAMES]
_LH_IDX = {n: i for i, n in enumerate(_LH_FRAMES)}
_RH_IDX = {n: i for i, n in enumerate(_RH_FRAMES)}
# Bone connectivity by joint ordinal (wrist=1 root; thumb has no intermediate;
# wrist->palm). A bone is drawn only when both endpoints are present this frame.
_HAND_BONES = [
    (1, 2), (2, 3), (3, 4), (4, 5),                 # thumb
    (1, 6), (6, 7), (7, 8), (8, 9), (9, 10),        # index
    (1, 11), (11, 12), (12, 13), (13, 14), (14, 15),  # middle
    (1, 16), (16, 17), (17, 18), (18, 19), (19, 20),  # ring
    (1, 21), (21, 22), (22, 23), (23, 24), (24, 25),  # little
    (1, 0),                                         # wrist -> palm
]


class HandSkeletonDeriver:
    """Turn a Quest 26-joint /skeleton/hand_* FrameTransforms into its SceneUpdate
    viz twin on .../scene — one 8mm sphere per tracked joint + a LINE_LIST of the
    bones (left cyan, right amber). Ported from visio-quest's kHandSceneSchema."""

    def __init__(self):
        self._scene_ch = {}
        self._last_ns = {}
        self._next_stream = _HAND_SCENE_BASE

    def derive(self, msg, ch):
        topic = ch.topic
        if ch.schema_name != _TF_SCHEMA:
            return None
        if topic.endswith("/hand_left"):
            idx, color, ent_id = _LH_IDX, (0.20, 0.80, 1.00), "hand_left"
        elif topic.endswith("/hand_right"):
            idx, color, ent_id = _RH_IDX, (1.00, 0.60, 0.20), "hand_right"
        else:
            return None
        ns = _ns(msg.timestamp)
        if ns - self._last_ns.get(topic, 0) < _SCENE_MIN_PERIOD_NS:
            return None                        # throttle scene to ~30 fps
        self._last_ns[topic] = ns
        fts = FrameTransforms()
        fts.ParseFromString(msg.payload)
        joints = [None] * 26
        for t in fts.transforms:
            o = idx.get(t.child_frame_id)
            if o is not None:
                joints[o] = (t.translation.x, t.translation.y, t.translation.z)
        if not any(joints):
            return None
        out_ch = self._scene_ch.get(topic)
        if out_ch is None:
            out_ch = make_channel(topic + "/scene", _SCENE_SCHEMA, stream_id=self._next_stream)
            self._next_stream += 1
            self._scene_ch[topic] = out_ch
        scene = _hand_scene(joints, ent_id, color, msg.timestamp)
        out = Message(stream_id=out_ch.id, payload=scene.SerializeToString())
        out.timestamp.CopyFrom(msg.timestamp)
        return out, out_ch


def _hand_scene(joints, entity_id, color, timestamp):
    r, g, b = color
    col = Color(r=r, g=g, b=b, a=1.0)
    spheres = []
    for pos in joints:
        if pos is None:
            continue
        spheres.append(SpherePrimitive(
            pose=Pose(position=Vector3(x=pos[0], y=pos[1], z=pos[2]), orientation=_UNIT_Q),
            size=Vector3(x=0.008, y=0.008, z=0.008), color=col))
    pts = []
    for ja, jb in _HAND_BONES:
        if joints[ja] is None or joints[jb] is None:
            continue
        for o in (ja, jb):
            p = joints[o]
            pts.append(Point3(x=p[0], y=p[1], z=p[2]))
    line = LinePrimitive(
        type=LinePrimitive.LINE_LIST,
        pose=Pose(position=Vector3(x=0, y=0, z=0), orientation=_UNIT_Q),
        thickness=0.004, scale_invariant=False, points=pts, color=col)
    ent = SceneEntity(timestamp=timestamp, frame_id="world", id=entity_id,
                      spheres=spheres, lines=[line])
    return SceneUpdate(entities=[ent])
