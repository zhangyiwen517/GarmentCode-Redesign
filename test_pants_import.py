#!/usr/bin/env python
"""Test script to check pants.py import and surface_stitches."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("Starting imports...", flush=True)

try:
    from pygarment.garmentcode.component import Component
    print("Component imported", flush=True)
except Exception as e:
    print(f"FAILED to import Component: {e}", flush=True)
    sys.exit(1)

try:
    from assets.garment_programs.pants import Pants
    print("Pants imported", flush=True)
except Exception as e:
    print(f"FAILED to import Pants: {e}", flush=True)
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("Creating Pants...", flush=True)
p = Pants()
print("Pants created", flush=True)

print("Running assembly...", flush=True)
pat = p.assembly()
print("Assembly done", flush=True)

print("Panels:", list(pat.pattern['panels'].keys()), flush=True)
print("Surface stitches count:", len(pat.pattern.get('surface_stitches', [])), flush=True)

for i, ss in enumerate(pat.pattern.get('surface_stitches', [])):
    print(f"Surface stitch {i}:", flush=True)
    print(f"  source: panel={ss['source']['panel']}, edge={ss['source']['edge']}", flush=True)
    print(f"  target: panel={ss['target']['panel']}, segment={ss['target']['segment']}", flush=True)
    print(f"  coordinate_space: {ss['target'].get('coordinate_space', 'none')}", flush=True)
    print(f"  type: {ss.get('type', 'none')}", flush=True)

print("Done!", flush=True)