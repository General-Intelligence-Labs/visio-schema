"""Compile a layout's scalar byte readouts and fixed display-only ranges."""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from visio_schema.display import hand_geometry as hg
from visio_schema.foxglove.PackedElementField_pb2 import PackedElementField as Field
from visio_schema.v1.calibration.tactile_pb2 import TactileLayout

# These are viewer defaults, not calibrated physical full scales. UINT8 keeps
# the original fabric-glove range; signed readings below zero clip to white.
_FORMATS = {
    Field.UINT8: (struct.Struct("<B"), hg.FULL_SCALE),
    Field.INT8: (struct.Struct("<b"), 127.0),
    Field.UINT16: (struct.Struct("<H"), 65535.0),
    Field.INT16: (struct.Struct("<h"), 32767.0),
    Field.UINT32: (struct.Struct("<I"), 4294967295.0),
    Field.INT32: (struct.Struct("<i"), 2147483647.0),
    Field.FLOAT32: (struct.Struct("<f"), 1.0),
    Field.FLOAT64: (struct.Struct("<d"), 1.0),
}


class TactileSceneError(ValueError):
    """Invalid tactile input; skip its visualization without stopping raw capture."""


def validate_range(value_range):
    """Return a finite increasing display range, or None for per-type defaults."""
    if value_range is None:
        return None
    lo, hi = value_range
    if not all(math.isfinite(v) for v in (lo, hi, hi - lo)) or hi <= lo:
        raise ValueError("tactile display range must have finite MIN < MAX and finite span")
    return lo, hi


@dataclass(frozen=True)
class _Readout:
    offset: int
    unpacker: struct.Struct
    lo: float
    span: float


@dataclass(frozen=True)
class ScalarLayout:
    readouts: tuple[_Readout, ...]
    required_bytes: int

    def validate_size(self, data):
        if len(data) < self.required_bytes:
            raise TactileSceneError(
                f"truncated tactile payload: need {self.required_bytes} bytes, got {len(data)}"
            )

    def intensities(self, data):
        self.validate_size(data)
        values = []
        for i, readout in enumerate(self.readouts):
            value, = readout.unpacker.unpack_from(data, readout.offset)
            if not math.isfinite(value):
                raise TactileSceneError(f"tactile point {i}: non-finite scalar reading")
            values.append(max(0.0, min(1.0, (value - readout.lo) / readout.span)))
        return values


def compile_layout(layout, value_range):
    """Select the sole scalar field per group; never guess an axis or its units."""
    if layout.precision not in (TactileLayout.PRECISION_UNSPECIFIED, TactileLayout.LOGICAL,
                                TactileLayout.APPROXIMATE, TactileLayout.MEASURED):
        raise TactileSceneError(f"unknown tactile layout precision {layout.precision}")
    if not layout.points:
        raise TactileSceneError("tactile layout has no points")
    groups = []
    for i, group in enumerate(layout.groups):
        if len(group.fields) != 1:
            raise TactileSceneError(
                f"tactile group {i}: expected one scalar field, got {len(group.fields)}"
            )
        field = group.fields[0]
        if field.type not in _FORMATS:
            raise TactileSceneError(f"tactile group {i}: unsupported NumericType {field.type}")
        unpacker, default_hi = _FORMATS[field.type]
        lo, hi = value_range if value_range is not None else (0.0, default_hi)
        groups.append((field.offset, unpacker, lo, hi - lo))
    readouts = []
    for i, point in enumerate(layout.points):
        if point.group >= len(groups):
            raise TactileSceneError(
                f"tactile point {i}: group {point.group} is outside layout groups"
            )
        if not all(math.isfinite(v) for v in (point.x, point.y, point.z)):
            raise TactileSceneError(f"tactile point {i}: non-finite layout coordinates")
        if (layout.precision in (TactileLayout.PRECISION_UNSPECIFIED, TactileLayout.LOGICAL)
                and point.part not in {"thumb", "palm", "index", "middle", "ring", "pinky"}):
            raise TactileSceneError(f"tactile point {i}: unknown LOGICAL part {point.part!r}")
        offset, unpacker, lo, span = groups[point.group]
        readouts.append(_Readout(point.index + offset, unpacker, lo, span))
    return ScalarLayout(tuple(readouts), max(r.offset + r.unpacker.size for r in readouts))
