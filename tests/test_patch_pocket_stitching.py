from assets.garment_programs import pockets
from assets.garment_programs.pants import PantsHalf, Pants


def _build_body_and_design():
    body = {
        'waist': 70.0,
        'hips': 96.0,
        'leg_circ': 40.0,
        'crotch_hip_diff': 10.0,
        '_hip_inclination': 10.0,
        '_waist_level': 20.0,
        '_leg_length': 1.0,
        'hip_back_width': 52.0,
        'waist_back_width': 36.0,
        'bum_points': 18.0,
    }
    design = {
        'flare': {'v': 1.0},
        'length': {'v': 80.0, 'range': [40.0, 120.0]},
        'width': {'v': 1.0},
        'rise': {'v': 0.5},
        'cuff': {'type': {'v': False}, 'cuff_len': {'v': 0.0}},
        'front_placket': {
            'enabled': {'v': True},
            'width': {'v': 4.0},
            'height': {'v': 10.0},
            'distance_from_waist': {'v': 5.0},
        },
        'back_patch_pocket': {
            'enabled': {'v': True},
            'pocket_width': {'v': 10.0},
            'pocket_depth': {'v': 10.0},
            'distance_from_waist': {'v': 5.0},
            'distance_from_side': {'v': 6.0},
        },
    }
    return body, design


def test_patch_pocket_creates_stitching_rule():
    body, design = _build_body_and_design()
    pants = PantsHalf(body, design, tag='t', rise=0.5)

    assert pants.back_pocket is not None
    assert 'pocket_attach' in pants.back.interfaces
    assert pants.stitching_rules.assembly()


def test_front_placket_creates_stitching_rule():
    body, design = _build_body_and_design()
    pants = PantsHalf(body, design, tag='t', rise=0.5)

    assert pants.front_placket is not None
    assert 'placket_attach' in pants.front.interfaces
    assert pants.stitching_rules.assembly()


def test_full_pants_has_single_front_placket():
    body, design = _build_body_and_design()
    pants = Pants(body, design, rise=0.5)

    assert pants.right.front_placket is None
    assert pants.left.front_placket is not None
    assert pants.stitching_rules.assembly()


def test_patch_pocket_supports_shape_types():
    pocket = pockets.PatchPocket('pocket_test', 8.0, 10.0, pocket_type='rounded')
    assert pocket.shape_type == 'rounded'

    pointy_pocket = pockets.PatchPocket('pocket_test_2', 8.0, 10.0, pocket_type='pointed')
    assert pointy_pocket.shape_type == 'pointed'
