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


def test_approximate_coordinates_pass_through_for_either_hand():
    lay = _layout(TactileLayout.APPROXIMATE,
                  [(0, "palm", 0.010, 0.020), (437, "index", 0.012, 0.150)])
    want = [(0, 0, 0.010, 0.020), (437, 437, 0.012, 0.150)]
    for is_left in (True, False):
        got = hg.build_placement(lay, is_left)
        assert [(i, t, round(x, 6), round(y, 6)) for i, t, x, y in got] == want


def test_logical_layout_is_mirrored_by_side():
    lay = _layout(TactileLayout.LOGICAL, [(5, "index", 1, 2)])
    (_, _, xl, yl), = hg.build_placement(lay, True)
    (_, _, xr, yr), = hg.build_placement(lay, False)
    assert yl == yr and xl != xr


def test_footprint_shrinks_to_a_dense_pitch_and_caps_at_cube():
    dense = [(0, 0, 0.0, 0.0), (1, 1, 0.004, 0.0)]
    assert abs(hg.footprint(dense) - 0.85 * 0.004) < 1e-9
    sparse = [(0, 0, 0.0, 0.0), (1, 1, 0.05, 0.0)]
    assert hg.footprint(sparse) == hg.CUBE
    assert hg.footprint([]) == hg.CUBE


def test_a_head_local_glove_root_names_its_side():
    assert hg.side_of_topic("/glove_left/tactile/0") == "left"
    assert hg.side_of_topic("/glove_right/tactile/0/layout") == "right"
    assert hg.side_of_topic("/glove_aB3xY9pQ/tactile/0") is None
