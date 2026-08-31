"""Quick debug - just check surface_stitches values"""
import sys, os
os.environ['PYGARMENT_VERBOSE'] = '0'
sys.path.insert(0, '/home/zhang/GarmentCode')

import numpy as np
from pygarment.pattern import rotation as rotation_tools

# Simulate what _compute_pocket_position does
# Use simplified values
back_trans = np.array([-5.0, -10.0, -20.0])
R = rotation_tools.euler_xyz_to_R(np.array([0.0, 0.0, 0.0]))
target_normal = R @ np.array([0.0, 0.0, 1.0])

pocket_width = 10.0
pocket_depth = 10.0
pocket_x = 15.0
pocket_y = 50.0

surface_offset_cm = 1.5
pocket_translation = (back_trans + target_normal * surface_offset_cm).tolist()
pocket_translation[0] += pocket_x - pocket_width / 2.0
pocket_translation[1] += pocket_y - pocket_depth / 2.0

print(f"back_trans: {back_trans}")
print(f"target_normal: {target_normal}")
print(f"pocket_translation: {pocket_translation}")

# Test pocket_point_to_target_2d
pocket_rotation = np.array([0.0, 0.0, 0.0])
target_rotation = np.array([0.0, 0.0, 0.0])

def pocket_point_to_target_2d(point_2d):
    point_3d = np.array([point_2d[0], point_2d[1], 0.0])
    R_pocket = rotation_tools.euler_xyz_to_R(np.asarray(pocket_rotation, dtype=float))
    R_target = rotation_tools.euler_xyz_to_R(np.asarray(target_rotation, dtype=float))
    world_point = R_pocket @ point_3d + np.asarray(pocket_translation, dtype=float)
    target_local = R_target.T @ (world_point - np.asarray(back_trans, dtype=float))
    return target_local[:2].tolist()

print("\nTarget segments (pre-pivot space):")
for edge_id, (start_local, end_local) in [
    (1, ([pocket_width, 0.0], [pocket_width, pocket_depth])),
    (2, ([pocket_width, pocket_depth], [0.0, pocket_depth])),
    (3, ([0.0, pocket_depth], [0.0, 0.0])),
]:
    start = pocket_point_to_target_2d(start_local)
    end = pocket_point_to_target_2d(end_local)
    print(f"  Edge {edge_id}: {start} -> {end}")

print("\nExpected pocket position on back panel:")
print(f"  x range: {pocket_x - pocket_width/2} to {pocket_x + pocket_width/2}")
print(f"  y range: {pocket_y - pocket_depth/2} to {pocket_y + pocket_depth/2}")