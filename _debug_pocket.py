"""Debug script to check surface_stitches output"""
import sys
sys.path.insert(0, '/home/zhang/GarmentCode')

from assets.garment_programs.pants import PantsHalf, Pants

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
        'enabled': {'v': False},
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

print("=" * 60)
print("Testing PantsHalf (right)")
print("=" * 60)
half = PantsHalf(body, design, tag='r', rise=0.5)

print(f"\nBack panel name: {half.back.name}")
print(f"Back panel translation: {half.back.translation}")
print(f"Back panel rotation: {half.back.rotation.as_euler('XYZ', degrees=True)}")

if half.back_pocket:
    print(f"\nPocket panel name: {half.back_pocket.panel.name}")
    print(f"Pocket panel translation: {half.back_pocket.panel.translation}")
    print(f"Pocket panel rotation: {half.back_pocket.panel.rotation.as_euler('XYZ', degrees=True)}")
    
    print(f"\nSurface stitches ({len(half.surface_stitches)}):")
    for i, ss in enumerate(half.surface_stitches):
        print(f"  Stitch {i}:")
        print(f"    source: panel={ss['source']['panel']}, edge={ss['source']['edge']}")
        print(f"    target: panel={ss['target']['panel']}")
        print(f"    target segment: {ss['target']['segment']}")
        print(f"    coordinate_space: {ss['target'].get('coordinate_space', 'N/A')}")
else:
    print("\nNo back pocket!")

print(f"\nBack panel pivot: {half._back_pivot}")

# Check assembly
print("\n" + "=" * 60)
print("Assembly output")
print("=" * 60)
pattern = half.assembly()
print(f"Panels: {list(pattern.pattern['panels'].keys())}")
print(f"Surface stitches in assembly: {pattern.pattern.get('surface_stitches', [])}")

# Check Pants
print("\n" + "=" * 60)
print("Testing Pants")
print("=" * 60)
pants = Pants(body, design, rise=0.5)
pattern2 = pants.assembly()
print(f"Panels: {list(pattern2.pattern['panels'].keys())}")
ss_list = pattern2.pattern.get('surface_stitches', [])
print(f"Surface stitches count: {len(ss_list)}")
for i, ss in enumerate(ss_list):
    print(f"  Stitch {i}:")
    print(f"    source: panel={ss['source']['panel']}, edge={ss['source']['edge']}")
    print(f"    target: panel={ss['target']['panel']}")
    print(f"    target segment: {ss['target']['segment']}")
    print(f"    coordinate_space: {ss['target'].get('coordinate_space', 'N/A')}")