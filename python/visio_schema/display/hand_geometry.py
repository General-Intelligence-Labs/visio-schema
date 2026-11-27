"""hand_geometry.py — tactile glove taxel geometry (for the tactile scene deriver).

Pure math over a TactileLayout. Under precision=LOGICAL (the JQ glove) each
Point's x/y carry the logical grid col/row, so the hand SHAPE is reconstructed
here from (part, row, col), mirroring for a left glove. Under APPROXIMATE or
MEASURED, x/y/z are already spatial coordinates in metres for that glove's own
hand, and are used as they are. No rendering deps —
TactileSceneDeriver builds on it.
"""
from __future__ import annotations

from math import dist

from visio_schema.v1.calibration.tactile_pb2 import TactileLayout

# ── hand geometry (metres) — an estimate over the logical layout ─────────────
# Tuned for the RIGHT hand (palm-up, looking down +z): index/middle/ring/pinky
# stand upright (tip = +y), spread along x; the thumb splays to the side, lower
# and shorter. A left glove is mirrored inside place().
FINGER_PITCH = 0.030   # between finger centers (index anchored)
X_INDEX = -0.030       # index's center x, kept fixed as the anchor
THUMB_X = -0.060       # thumb block x (beside the fingers)
COL_DX = 0.008         # between columns within a finger / the thumb (3 cols)
ROW_DY = 0.018         # between rows within a finger (4 rows ~54mm)
ROOT_Y = 0.020         # finger roots at this y; tips extend to +y
PALM_DX = 0.008
PALM_DY = 0.014

# render params
FULL_SCALE = 80.0   # pressure value mapped to full red
POINT_SIZE = 0.007  # taxel marker diameter (m)

_FINGER_SLOT = {"index": 0, "middle": 1, "ring": 2, "pinky": 3}

# Precisions whose x/y are real coordinates (metres), not grid col/row.
_PLACED = (TactileLayout.APPROXIMATE, TactileLayout.MEASURED)


def place(part, row, col, kind, is_left):
    """Logical (part,row,col) -> (x,y) on the hand plane.

    Tuned for the RIGHT hand (palm-up, thumb on the left). A LEFT glove ships the
    SAME col/row layout, so we mirror only the finger/thumb CENTER (thumb -> right,
    pinky -> left) while KEEPING each row's column direction — that way the vendor
    taxel numbering still reads left-to-right within a finger (pinky row0 = 31 30
    29, not 29 30 31). A blind `x = -x` would flip the columns too, reversing it.
    """
    if part == "palm":
        # Palm below the finger roots. Row 0 has 12 cols (rows 1-4 have 15); the
        # missing 3 belong under the THUMB, so leave the gap on the thumb side —
        # the left on a right hand, the right on a left hand.
        x_off = 3 if (row == 0 and not is_left) else 0
        return (col - 7 + x_off) * PALM_DX, ROOT_Y - 0.030 - row * PALM_DY
    if part == "thumb":
        # Beside the fingers (left hand -> right side); raised so the thumb bend
        # lines up horizontally with the palm's top row.
        THUMB_UP = -0.016
        base = -THUMB_X if is_left else THUMB_X
        if kind == "bend":
            return base, ROOT_Y + THUMB_UP - 0.014
        return base + (col - 1) * COL_DX, ROOT_Y + (3 - row) * ROW_DY + THUMB_UP
    # index / middle / ring / pinky: upright, index anchored; +1 row above the palm.
    fx = X_INDEX + _FINGER_SLOT[part] * FINGER_PITCH
    if is_left:
        fx = -fx  # mirror the finger center only; columns stay left-to-right
    if kind == "bend":
        return fx, ROOT_Y - 0.012 + ROW_DY
    # pressure: row 0 = fingertip (far, +y), row 3 = root (near).
    return fx + (col - 1) * COL_DX, ROOT_Y + (3 - row) * ROW_DY + ROW_DY


def _point_kind(layout, group_idx):
    """Point.group -> the readout kind ("pressure"/"bend"/...) via the group's
    first field name (TactileLayout Group.fields are foxglove.PackedElementFields
    whose `name` is the kind)."""
    g = layout.groups[group_idx]
    return g.fields[0].name if g.fields else ""


def build_placement(layout, is_left):
    """TactileLayout -> [(x, y, z)] in point order.

    Point.index is a payload byte offset; scalar decoding is separate. An
    APPROXIMATE or MEASURED layout already says where each taxel is, for its own
    hand, so its x/y/z pass through and `is_left` is not consulted. Under LOGICAL (and an
    unset precision, which predates the field) Point.x/y are the grid col/row,
    so we reconstruct the hand shape with place(part, row, col, kind); kind
    comes from the point's group. Both JQ hands ship the SAME col/row layout
    (only taxel_id/index differ); place() handles the left/right mirror.
    """
    if layout.precision in _PLACED:
        return [(pt.x, pt.y, pt.z) for pt in layout.points]
    out = []
    for pt in layout.points:
        kind = _point_kind(layout, pt.group)
        col, row = round(pt.x), round(pt.y)
        x, y = place(pt.part, row, col, kind, is_left)
        out.append((x, y, 0.0))
    return out


def footprint(placement):
    """The marker diameter (m): POINT_SIZE, shrunk to 85% of the tightest
    3D taxel pitch so a denser layout's points don't overlap."""
    pitch = min(
        (dist(a, b) for i, a in enumerate(placement) for b in placement[i + 1:] if a != b),
        default=POINT_SIZE / 0.85)
    return min(POINT_SIZE, 0.85 * pitch)


def side_of_topic(topic):
    """The glove side a topic belongs to, or None when the unit is unassigned.

    The root is one segment, ``<role>_<side>`` for an assigned limb
    (``/glove_left/...``) — see visio-schema/docs/protocol/stream_type_map.md.
    An UNASSIGNED limb roots at ``<role>_<code8>`` instead, which names no side.

    None, not False: taxel geometry is mirrored about the side, so guessing here
    renders the wrong hand rather than rendering nothing. The caller must refuse.
    """
    root = topic.strip("/").split("/")[0]
    _, sep, suffix = root.partition("_")
    if not sep or suffix not in ("left", "right"):
        return None
    return suffix
