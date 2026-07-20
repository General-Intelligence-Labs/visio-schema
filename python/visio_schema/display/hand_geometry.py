"""hand_geometry.py — JQ glove taxel -> hand-plane geometry (for the tactile scene deriver).

Pure math over a TactileLayout (precision=LOGICAL, so each Point's x/y carry the
logical grid col/row): reconstruct where each taxel sits on a hand-shaped plane,
mirroring the layout for a left glove. No rendering deps — TactileSceneDeriver
builds on it. The hand SHAPE is reconstructed here from (part, row, col); swapping
to an APPROXIMATE/MEASURED layout later would drop place() and use x/y directly.
"""
from __future__ import annotations

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
FULL_SCALE = 80.0   # pressure value mapped to full height / full red
MAX_H = 0.05        # tallest bar (m)
CUBE = 0.007        # taxel footprint (m)

_FINGER_SLOT = {"index": 0, "middle": 1, "ring": 2, "pinky": 3}


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
    """TactileLayout -> [(payload_index, taxel_id, x, y)] via place().

    Single-axis v1, so the payload offset is just Point.index. Under
    precision=LOGICAL, Point.x/y are the grid col/row, so we reconstruct the hand
    shape with place(part, row, col, kind); kind comes from the point's group.
    Both hands ship the SAME col/row layout (only taxel_id/index differ); place()
    handles the left/right mirror internally.
    """
    out = []
    for pt in layout.points:
        kind = _point_kind(layout, pt.group)
        col, row = int(round(pt.x)), int(round(pt.y))
        x, y = place(pt.part, row, col, kind, is_left)
        out.append((pt.index, pt.taxel_id, x, y))
    return out


def side_is_left(topic):
    """True if a topic belongs to the left glove (/glove_left/...)."""
    return "glove_left" in topic
