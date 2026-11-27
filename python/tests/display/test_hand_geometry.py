"""hand_geometry: LOGICAL layouts are reshaped into a hand, real-coordinate
layouts (APPROXIMATE / MEASURED) are drawn where they say."""
from visio_schema.display import hand_geometry as hg
from visio_schema.v1.calibration.tactile_pb2 import TactileLayout


def _layout(precision, points):
    lay = TactileLayout(precision=precision)
    g = lay.groups.add()
    g.fields.add(name="pressure", type=1, offset=0)
    for index, part, x, y in points:
        lay.points.add(group=0, taxel_id=index, index=index, part=part, x=x, y=y)
    return lay


def test_real_coordinates_pass_through_including_z_for_either_hand():
    for precision in (TactileLayout.APPROXIMATE, TactileLayout.MEASURED):
        lay = _layout(precision,
                      [(0, "palm", 0.010, 0.020), (437, "index", 0.012, 0.150)])
        lay.points[0].z, lay.points[1].z = 0.015, -0.025
        want = [(0.010, 0.020, 0.015), (0.012, 0.150, -0.025)]
        for is_left in (True, False):
            got = hg.build_placement(lay, is_left)
            assert [tuple(round(v, 6) for v in position) for position in got] == want


def test_logical_layout_is_mirrored_by_side():
    lay = _layout(TactileLayout.LOGICAL, [(5, "index", 1, 2)])
    lay.points[0].z = 123.0
    (xl, yl, zl), = hg.build_placement(lay, True)
    (xr, yr, zr), = hg.build_placement(lay, False)
    assert yl == yr and xl != xr
    assert zl == zr == 0


def test_footprint_uses_3d_pitch_and_caps_at_marker_size():
    for dense in ([(0.0, 0.0, 0.0), (0.004, 0.0, 0.0)],
                  [(0.0, 0.0, 0.0), (0.0, 0.0, 0.004)]):
        assert abs(hg.footprint(dense) - 0.85 * 0.004) < 1e-9
    sparse = [(0.0, 0.0, 0.0), (0.05, 0.0, 0.0)]
    assert hg.footprint(sparse) == hg.POINT_SIZE
    assert hg.footprint([]) == hg.POINT_SIZE


def test_a_head_local_glove_root_names_its_side():
    assert hg.side_of_topic("/glove_left/tactile/0") == "left"
    assert hg.side_of_topic("/glove_right/tactile/0/layout") == "right"
    assert hg.side_of_topic("/glove_aB3xY9pQ/tactile/0") is None
