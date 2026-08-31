"""Semantic edits for GarmentCode/SewingLDM pattern specification JSON.

The functions in this module work on the raw specification dictionary and
preserve unknown fields by copying and editing only the small pieces of geometry
that a command touches.
"""

from __future__ import annotations

import copy
import json
import math
import re
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import trimesh

from pygarment.meshgen.boxmeshgen import BoxMesh
from pygarment.meshgen.simulation import run_sim
from pygarment.meshgen.sim_config import PathCofig
from pygarment.pattern import utils as pattern_utils
from pygarment.pattern import rotation as rotation_tools
from pygarment.pattern.wrappers import VisPattern
import pygarment.data_config as data_config


SpecDict = Dict[str, Any]


PANEL_LABELS = {"LFP", "RFP", "LBP", "RBP"}
EDGE_LABELS = {"CF", "CB", "SH", "SS", "AH", "NECK", "HEL", "Dart"}
PANEL_CODE_MAP = {
    "LFP": "LFP",
    "RFP": "RFP",
    "LBP": "LBP",
    "RBP": "RBP",
}
EDGE_CODE_MAP = {
    "CF": "CF",
    "CB": "CB",
    "SH": "SH",
    "SPL": "SH",
    "BPL": "SH",
    "SS": "SS",
    "FAH": "AH",
    "BAH": "AH",
    "AH": "AH",
    "FNL": "NECK",
    "BNL": "NECK",
    "NECK": "NECK",
    "HEL": "HEL",
    "Dart": "Dart",
}


class SemanticEditError(RuntimeError):
    """Raised when a semantic command cannot be applied safely."""


@dataclass
class EditResult:
    spec: SpecDict
    report: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class SimulationResult:
    success: bool
    message: str
    out_dir: Optional[Path] = None
    sim_mesh_path: Optional[Path] = None
    glb_path: Optional[Path] = None
    render_front_path: Optional[Path] = None
    render_back_path: Optional[Path] = None
    traceback_text: Optional[str] = None


def _as_array(point: Sequence[float]) -> np.ndarray:
    return np.asarray(point, dtype=float)


def _as_list(point: np.ndarray) -> List[float]:
    return [float(point[0]), float(point[1])]


def _unit(vec: np.ndarray, context: str) -> np.ndarray:
    length = np.linalg.norm(vec)
    if length < 1e-8:
        raise SemanticEditError(f"Cannot normalize zero-length vector for {context}")
    return vec / length


def _rotate(vec: np.ndarray, angle_deg: float) -> np.ndarray:
    rad = math.radians(angle_deg)
    c, s = math.cos(rad), math.sin(rad)
    return np.asarray([c * vec[0] - s * vec[1], s * vec[0] + c * vec[1]])


def _angle_deg(v1: np.ndarray, v2: np.ndarray) -> float:
    n1 = np.linalg.norm(v1)
    n2 = np.linalg.norm(v2)
    if n1 < 1e-8 or n2 < 1e-8:
        return float("nan")
    cos = float(np.dot(v1, v2) / (n1 * n2))
    cos = max(min(cos, 1.0), -1.0)
    return float(math.degrees(math.acos(cos)))


def _distance_point_to_segment(point: np.ndarray, start: np.ndarray, end: np.ndarray) -> float:
    seg = end - start
    denom = float(np.dot(seg, seg))
    if denom < 1e-8:
        return float(np.linalg.norm(point - start))
    t = max(0.0, min(1.0, float(np.dot(point - start, seg) / denom)))
    closest = start + t * seg
    return float(np.linalg.norm(point - closest))


def _read_json_bytes(data: bytes) -> SpecDict:
    try:
        return json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SemanticEditError("Uploaded pattern JSON must be UTF-8 encoded") from exc
    except json.JSONDecodeError as exc:
        raise SemanticEditError(f"Invalid JSON: {exc}") from exc


def _ensure_spec_shape(spec: SpecDict) -> None:
    if not isinstance(spec, dict) or "pattern" not in spec:
        raise SemanticEditError('Pattern JSON must contain a top-level "pattern" object')
    pattern = spec["pattern"]
    if not isinstance(pattern, dict) or "panels" not in pattern:
        raise SemanticEditError('Pattern JSON must contain "pattern.panels"')
    if "stitches" not in pattern:
        pattern["stitches"] = []
    spec.setdefault("parameters", {})
    spec.setdefault("parameter_order", [])
    spec.setdefault(
        "properties",
        {
            "curvature_coords": "relative",
            "normalize_panel_translation": False,
            "normalized_edge_loops": True,
            "units_in_meter": 100,
        },
    )


def _prediction_code(item: SpecDict) -> Optional[str]:
    prediction = item.get("semantic_prediction")
    if isinstance(prediction, dict):
        code = prediction.get("code")
        return str(code) if code is not None else None
    if isinstance(prediction, str):
        return prediction
    return None


def _panel_semantic_label(panel: SpecDict) -> Optional[str]:
    if panel.get("semantic_label"):
        return str(panel["semantic_label"])
    code = _prediction_code(panel)
    if code:
        return PANEL_CODE_MAP.get(code, code)
    return None


def _edge_semantic_label(edge: SpecDict) -> Optional[str]:
    if edge.get("semantic_label"):
        return str(edge["semantic_label"])
    code = _prediction_code(edge)
    if code:
        return EDGE_CODE_MAP.get(code, code)
    return None


def _edge_endpoint_set(edge: SpecDict) -> set[int]:
    return {int(idx) for idx in edge.get("endpoints", [])}


def _edge_length(panel: SpecDict, edge_id: int) -> float:
    vertices = np.asarray(panel["vertices"], dtype=float)
    start_idx, end_idx = panel["edges"][edge_id]["endpoints"]
    return float(np.linalg.norm(vertices[end_idx] - vertices[start_idx]))


def _repair_split_armhole_labels(panel: SpecDict, explicit_edge_labels: Sequence[bool]) -> None:
    """Relabel obvious curved armhole continuations that were predicted as SS.

    Some front panels arrive as side seam + long curved armhole + tiny FAH
    connector, with the long curved part misclassified as SS. Endpoint rules
    need that curved continuation to participate as AH while preserving the
    actual straight side seam as SS.
    """

    edges = panel.get("edges", [])
    ah_ids = [idx for idx, edge in enumerate(edges) if _edge_semantic_label(edge) == "AH"]
    ss_ids = [idx for idx, edge in enumerate(edges) if _edge_semantic_label(edge) == "SS"]
    if not ah_ids or len(ss_ids) < 2:
        return

    ah_vertices = set()
    for edge_id in ah_ids:
        ah_vertices.update(_edge_endpoint_set(edges[edge_id]))

    for edge_id in ss_ids:
        if edge_id < len(explicit_edge_labels) and explicit_edge_labels[edge_id]:
            continue
        edge = edges[edge_id]
        if "curvature" not in edge or not (_edge_endpoint_set(edge) & ah_vertices):
            continue
        other_ss_ids = [idx for idx in ss_ids if idx != edge_id]
        has_separate_side_seam = any(
            "curvature" not in edges[idx] or not (_edge_endpoint_set(edges[idx]) & ah_vertices)
            for idx in other_ss_ids
        )
        if has_separate_side_seam:
            edge["semantic_label"] = "AH"


def normalize_semantic_predictions(spec: SpecDict) -> SpecDict:
    """Copy prediction codes into semantic_label when explicit labels are absent.

    The original semantic_prediction records are preserved. Existing
    semantic_label fields are never overwritten.
    """

    _ensure_spec_shape(spec)
    panels = spec["pattern"]["panels"]
    panel_order = spec["pattern"].get("panel_order")
    if isinstance(panel_order, list):
        spec["pattern"]["panel_order"] = [
            panel_name for panel_name in panel_order
            if panel_name is not None and panel_name in panels
        ]
    for panel in spec["pattern"]["panels"].values():
        if "semantic_label" not in panel:
            label = _panel_semantic_label(panel)
            if label:
                panel["semantic_label"] = label
        explicit_edge_labels = ["semantic_label" in edge for edge in panel.get("edges", [])]
        for edge in panel.get("edges", []):
            if "semantic_label" not in edge:
                label = _edge_semantic_label(edge)
                if label:
                    edge["semantic_label"] = label
        _repair_split_armhole_labels(panel, explicit_edge_labels)
    return spec


def build_semantic_index_summary(spec: SpecDict) -> Dict[str, Any]:
    _ensure_spec_shape(spec)
    panels: Dict[str, List[str]] = {}
    edges: Dict[str, Dict[str, List[int]]] = {}
    detected_panel_labels = []
    for panel_name, panel in spec["pattern"]["panels"].items():
        panel_label = _panel_semantic_label(panel) or panel_name
        panels.setdefault(panel_label, []).append(panel_name)
        detected_panel_labels.append(panel_label)
        edge_summary: Dict[str, List[int]] = {}
        for edge_id, edge in enumerate(panel.get("edges", [])):
            edge_label = _edge_semantic_label(edge)
            if edge_label:
                edge_summary.setdefault(edge_label, []).append(edge_id)
        edges[panel_label] = edge_summary
    return {
        "panels": panels,
        "edges": edges,
        "detected_panel_labels": detected_panel_labels,
    }


def _panel_display_label(panel_name: str, panel: SpecDict) -> str:
    return str(_panel_semantic_label(panel) or panel_name)


def _panel_matches(panel_name: str, panel: SpecDict, target: str) -> bool:
    return panel_name == target or _panel_semantic_label(panel) == target


def _edge_matches(edge: SpecDict, target: str) -> bool:
    return _edge_semantic_label(edge) == target


def _iter_target_panels(spec: SpecDict, target_panels: Iterable[str]) -> List[Tuple[str, SpecDict]]:
    panels = spec["pattern"]["panels"]
    resolved: List[Tuple[str, SpecDict]] = []
    missing = []
    for target in target_panels:
        matches = [(name, panel) for name, panel in panels.items() if _panel_matches(name, panel, target)]
        if not matches:
            missing.append(target)
            continue
        resolved.extend(matches)
    if missing:
        summary = build_semantic_index_summary(spec)
        detected = ", ".join(sorted(set(summary["detected_panel_labels"]))) or "none"
        raise SemanticEditError(
            "JSON is loaded, but required panel semantic_label values were not found: "
            f"{', '.join(missing)}. Please check each panel has semantic_label "
            "LFP/RFP/LBP/RBP, or semantic_prediction.code with one of those values. "
            f"Detected panel labels: {detected}."
        )

    deduped: List[Tuple[str, SpecDict]] = []
    seen = set()
    for name, panel in resolved:
        if name not in seen:
            seen.add(name)
            deduped.append((name, panel))
    return deduped


def _candidate_edge_ids(panel: SpecDict, semantic_label: str) -> List[int]:
    return [idx for idx, edge in enumerate(panel.get("edges", [])) if _edge_matches(edge, semantic_label)]


def _find_single_edge(panel: SpecDict, panel_name: str, semantic_label: str) -> int:
    matches = _candidate_edge_ids(panel, semantic_label)
    if not matches:
        raise SemanticEditError(f"{panel_name}: no edge with semantic_label={semantic_label}")
    if len(matches) > 1:
        raise SemanticEditError(
            f"{panel_name}: found {len(matches)} edges with semantic_label={semantic_label}; "
            "this edit needs a unique target edge"
        )
    return matches[0]


def _find_edge_near_reference(panel: SpecDict, panel_name: str, semantic_label: str, reference_edge_id: int) -> int:
    matches = _candidate_edge_ids(panel, semantic_label)
    if not matches:
        raise SemanticEditError(f"{panel_name}: no edge with semantic_label={semantic_label}")
    if len(matches) == 1:
        return matches[0]

    vertices = np.asarray(panel["vertices"], dtype=float)
    ref_edge = panel["edges"][reference_edge_id]
    ref_start, ref_end = [vertices[idx] for idx in ref_edge["endpoints"]]

    def score(edge_id: int) -> Tuple[float, float]:
        edge = panel["edges"][edge_id]
        start, end = [vertices[idx] for idx in edge["endpoints"]]
        dist = min(
            _distance_point_to_segment(start, ref_start, ref_end),
            _distance_point_to_segment(end, ref_start, ref_end),
        )
        length = float(np.linalg.norm(end - start))
        return dist, -length

    return sorted(matches, key=score)[0]


def _find_reference_edge_for_base(panel: SpecDict, panel_name: str, reference_label: str, base_label: str) -> int:
    matches = _candidate_edge_ids(panel, reference_label)
    if not matches:
        raise SemanticEditError(f"{panel_name}: no edge with semantic_label={reference_label}")
    if len(matches) == 1:
        return matches[0]

    base_matches = _candidate_edge_ids(panel, base_label)
    if not base_matches:
        raise SemanticEditError(f"{panel_name}: no edge with semantic_label={base_label}")
    vertices = np.asarray(panel["vertices"], dtype=float)
    base_points = []
    for edge_id in base_matches:
        edge = panel["edges"][edge_id]
        base_points.extend(vertices[idx] for idx in edge["endpoints"])

    def score(ref_id: int) -> float:
        ref_edge = panel["edges"][ref_id]
        ref_start, ref_end = [vertices[idx] for idx in ref_edge["endpoints"]]
        return min(_distance_point_to_segment(point, ref_start, ref_end) for point in base_points)

    return sorted(matches, key=score)[0]


def _find_shared_edge_pair(panel: SpecDict, panel_name: str, line_label: str, curve_label: str) -> Tuple[int, int, int]:
    line_ids = _candidate_edge_ids(panel, line_label)
    curve_ids = _candidate_edge_ids(panel, curve_label)
    if not line_ids:
        raise SemanticEditError(f"{panel_name}: no edge with semantic_label={line_label}")
    if not curve_ids:
        raise SemanticEditError(f"{panel_name}: no edge with semantic_label={curve_label}")

    pairs = []
    for line_id in line_ids:
        line_endpoints = set(panel["edges"][line_id]["endpoints"])
        for curve_id in curve_ids:
            curve_endpoints = set(panel["edges"][curve_id]["endpoints"])
            shared = line_endpoints & curve_endpoints
            if len(shared) == 1:
                pairs.append((line_id, curve_id, next(iter(shared))))

    if not pairs:
        raise SemanticEditError(f"{panel_name}: {line_label} and {curve_label} do not share one vertex")
    if len(pairs) > 1:
        raise SemanticEditError(
            f"{panel_name}: {line_label} and {curve_label} have {len(pairs)} possible shared endpoints"
        )
    return pairs[0]


def _smooth_short_armhole_connector(
    spec: SpecDict,
    panel: SpecDict,
    curve_edge_id: int,
    curve_label: str,
    shared_vertex: int,
    tangent: np.ndarray,
    threshold_cm: float,
) -> Dict[str, Optional[int]]:
    result = {"moved_connector_vertex": None, "smoothed_curve_edge_id": None}
    if curve_label != "AH":
        return result
    threshold = threshold_cm * _pattern_units_per_cm(spec)
    if _edge_length(panel, curve_edge_id) > threshold:
        return result

    edge = panel["edges"][curve_edge_id]
    start_idx, end_idx = edge["endpoints"]
    other_vertex = end_idx if shared_vertex == start_idx else start_idx
    continuation_ids = [
        edge_id
        for edge_id in range(len(panel.get("edges", [])))
        if (
            edge_id != curve_edge_id
            and other_vertex in panel["edges"][edge_id].get("endpoints", [])
            and _edge_semantic_label(panel["edges"][edge_id]) == curve_label
        )
    ]
    if not continuation_ids:
        return result
    continuation_id = max(continuation_ids, key=lambda edge_id: _edge_length(panel, edge_id))

    vertices = np.asarray(panel["vertices"], dtype=float)
    shared = vertices[shared_vertex]
    old_other = vertices[other_vertex]
    connector_len = float(np.linalg.norm(old_other - shared))
    if connector_len < 1e-8:
        return result

    connector_unit = _unit(tangent, "short armhole connector")
    panel["vertices"][other_vertex] = _as_list(shared + connector_unit * connector_len)
    vertices = np.asarray(panel["vertices"], dtype=float)
    other = vertices[other_vertex]

    current_tangent, current_cp_index, _ = _curve_tangent_at_shared(spec, panel, curve_edge_id, other_vertex)
    current_len = float(np.linalg.norm(current_tangent))
    if current_len > 1e-8:
        _write_curve_control(
            spec,
            panel,
            curve_edge_id,
            other_vertex,
            other - connector_unit * current_len,
            current_cp_index,
        )

    continuation_tangent, continuation_cp_index, _ = _curve_tangent_at_shared(
        spec, panel, continuation_id, other_vertex
    )
    continuation_len = float(np.linalg.norm(continuation_tangent))
    if continuation_len > 1e-8:
        _write_curve_control(
            spec,
            panel,
            continuation_id,
            other_vertex,
            other + connector_unit * continuation_len,
            continuation_cp_index,
        )

    result["moved_connector_vertex"] = int(other_vertex)
    result["smoothed_curve_edge_id"] = int(continuation_id)
    return result


def _short_connector_move_vertex(
    spec: SpecDict,
    panel: SpecDict,
    curve_edge_id: int,
    curve_label: str,
    shared_vertex: int,
    tangent: np.ndarray,
    threshold_cm: float,
) -> Optional[int]:
    """Backward-compatible wrapper for older direct callers."""

    return _smooth_short_armhole_connector(
        spec,
        panel,
        curve_edge_id,
        curve_label,
        shared_vertex,
        tangent,
        threshold_cm,
    )["moved_connector_vertex"]


def _has_same_curve_continuation(panel: SpecDict, curve_edge_id: int, curve_label: str, vertex_id: int) -> bool:
    return any(
        edge_id != curve_edge_id
        and vertex_id in panel["edges"][edge_id].get("endpoints", [])
        and _edge_semantic_label(panel["edges"][edge_id]) == curve_label
        for edge_id in range(len(panel.get("edges", [])))
    )


def _pattern_units_per_cm(spec: SpecDict) -> float:
    units_in_meter = spec.get("properties", {}).get("units_in_meter", 100)
    try:
        return float(units_in_meter) / 100.0
    except (TypeError, ValueError) as exc:
        raise SemanticEditError(f"Invalid properties.units_in_meter: {units_in_meter}") from exc


def _curvature_coords(spec: SpecDict) -> str:
    return spec.get("properties", {}).get("curvature_coords", "relative")


def _control_abs(start: np.ndarray, end: np.ndarray, control: Sequence[float], coords: str) -> np.ndarray:
    point = _as_array(control)
    if coords == "absolute":
        return point
    return pattern_utils.rel_to_abs_2d(start, end, point)


def _control_to_storage(start: np.ndarray, end: np.ndarray, point_abs: np.ndarray, coords: str) -> List[float]:
    if coords == "absolute":
        return _as_list(point_abs)
    return _as_list(pattern_utils.abs_to_rel_2d(start, end, point_abs))


def _curve_tangent_at_shared(
    spec: SpecDict,
    panel: SpecDict,
    curve_edge_id: int,
    shared_vertex: int,
) -> Tuple[np.ndarray, Optional[int], str]:
    vertices = np.asarray(panel["vertices"], dtype=float)
    edge = panel["edges"][curve_edge_id]
    start_idx, end_idx = edge["endpoints"]
    start = vertices[start_idx]
    end = vertices[end_idx]
    coords = _curvature_coords(spec)

    if "curvature" not in edge:
        other = end if shared_vertex == start_idx else start
        shared = vertices[shared_vertex]
        return other - shared, None, "line"

    curvature = edge["curvature"]
    if isinstance(curvature, list):
        control = _control_abs(start, end, curvature, coords)
        shared = vertices[shared_vertex]
        return control - shared, 0, "legacy_quadratic"

    curve_type = curvature.get("type")
    params = curvature.get("params", [])
    if curve_type == "quadratic":
        if not params:
            raise SemanticEditError(f"Curve edge {curve_edge_id} has empty quadratic params")
        control = _control_abs(start, end, params[0], coords)
        shared = vertices[shared_vertex]
        return control - shared, 0, "quadratic"
    if curve_type == "cubic":
        if len(params) < 2:
            raise SemanticEditError(f"Curve edge {curve_edge_id} has incomplete cubic params")
        cp_index = 0 if shared_vertex == start_idx else 1
        control = _control_abs(start, end, params[cp_index], coords)
        shared = vertices[shared_vertex]
        return control - shared, cp_index, "cubic"

    raise SemanticEditError(f"Unsupported curve type on edge {curve_edge_id}: {curve_type}")


def _write_curve_control(
    spec: SpecDict,
    panel: SpecDict,
    curve_edge_id: int,
    shared_vertex: int,
    control_abs: np.ndarray,
    cp_index: Optional[int],
) -> str:
    edge = panel["edges"][curve_edge_id]
    vertices = np.asarray(panel["vertices"], dtype=float)
    start_idx, end_idx = edge["endpoints"]
    start = vertices[start_idx]
    end = vertices[end_idx]
    coords = _curvature_coords(spec)
    stored = _control_to_storage(start, end, control_abs, coords)

    if "curvature" not in edge:
        edge["curvature"] = {"type": "quadratic", "params": [stored]}
        return "created_quadratic_control_point"

    curvature = edge["curvature"]
    if isinstance(curvature, list):
        edge["curvature"] = stored
        return "curve_control_point"

    curve_type = curvature.get("type")
    if curve_type == "quadratic":
        curvature.setdefault("params", [stored])
        curvature["params"][0] = stored
        return "curve_control_point"
    if curve_type == "cubic":
        if cp_index is None:
            cp_index = 0 if shared_vertex == start_idx else 1
        curvature.setdefault("params", [stored, stored])
        curvature["params"][cp_index] = stored
        return "curve_control_point"
    raise SemanticEditError(f"Unsupported curve type on edge {curve_edge_id}: {curve_type}")


def _next_dart_id(spec: SpecDict) -> str:
    max_id = 0
    for panel in spec["pattern"]["panels"].values():
        for edge in panel.get("edges", []):
            dart_id = edge.get("dart_id")
            if not isinstance(dart_id, str):
                continue
            match = re.fullmatch(r"dart_(\d+)", dart_id)
            if match:
                max_id = max(max_id, int(match.group(1)))
    return f"dart_{max_id + 1:03d}"


def _copy_base_segment(edge: SpecDict, endpoints: Sequence[int]) -> SpecDict:
    new_edge = copy.deepcopy(edge)
    new_edge["endpoints"] = list(endpoints)
    new_edge.pop("curvature", None)
    return new_edge


def _dart_leg(endpoints: Sequence[int], dart_id: str, role: str) -> SpecDict:
    return {
        "endpoints": list(endpoints),
        "semantic_label": "Dart",
        "dart_id": dart_id,
        "dart_role": role,
    }


def _remap_stitches_after_edge_replacement(
    spec: SpecDict,
    panel_name: str,
    replaced_edge_id: int,
    new_edge_count: int,
    old_edge_count: int = 1,
) -> List[str]:
    delta = new_edge_count - old_edge_count
    warnings = []
    for stitch_id, stitch in enumerate(spec["pattern"].get("stitches", [])):
        if not isinstance(stitch, list):
            continue
        for side in stitch:
            if not isinstance(side, dict) or side.get("panel") != panel_name or "edge" not in side:
                continue
            edge_id = side["edge"]
            if edge_id < replaced_edge_id:
                continue
            if edge_id == replaced_edge_id:
                warnings.append(
                    f"stitch {stitch_id} on {panel_name}.{replaced_edge_id} was mapped to the first replacement segment"
                )
                side["edge"] = replaced_edge_id
            elif edge_id > replaced_edge_id:
                side["edge"] = edge_id + delta
    return warnings


def _remove_stitches_touching_edge_range(
    spec: SpecDict,
    panel_name: str,
    start_edge_id: int,
    old_edge_count: int,
    new_edge_count: int,
) -> List[str]:
    delta = new_edge_count - old_edge_count
    warnings = []
    kept_stitches = []
    removed_start = start_edge_id
    removed_end = start_edge_id + old_edge_count

    for stitch_id, stitch in enumerate(spec["pattern"].get("stitches", [])):
        if not isinstance(stitch, list):
            kept_stitches.append(stitch)
            continue

        touches_removed_edge = False
        for side in stitch:
            if not isinstance(side, dict) or side.get("panel") != panel_name or "edge" not in side:
                continue
            edge_id = side["edge"]
            if removed_start <= edge_id < removed_end:
                touches_removed_edge = True
                break

        if touches_removed_edge:
            warnings.append(f"removed stitch {stitch_id} referencing deleted dart edges on {panel_name}")
            continue

        for side in stitch:
            if not isinstance(side, dict) or side.get("panel") != panel_name or "edge" not in side:
                continue
            if side["edge"] >= removed_end:
                side["edge"] += delta
        kept_stitches.append(stitch)

    spec["pattern"]["stitches"] = kept_stitches
    return warnings


def _remap_stitches_after_dart_removal(
    spec: SpecDict,
    panel_name: str,
    start_edge_id: int,
    old_edge_count: int,
    new_edge_count: int = 1,
) -> List[str]:
    """Preserve stitches on the restored base edge and drop dart-leg stitches."""
    delta = new_edge_count - old_edge_count
    warnings = []
    kept_stitches = []
    removed_start = start_edge_id
    removed_end = start_edge_id + old_edge_count
    first_dart_leg = start_edge_id + 1
    last_dart_leg = start_edge_id + old_edge_count - 2

    for stitch_id, stitch in enumerate(spec["pattern"].get("stitches", [])):
        if not isinstance(stitch, list):
            kept_stitches.append(stitch)
            continue

        remove_stitch = False
        for side in stitch:
            if not isinstance(side, dict) or side.get("panel") != panel_name or "edge" not in side:
                continue
            edge_id = side["edge"]
            if first_dart_leg <= edge_id <= last_dart_leg:
                remove_stitch = True
                break

        if remove_stitch:
            warnings.append(f"removed stitch {stitch_id} referencing deleted dart legs on {panel_name}")
            continue

        for side in stitch:
            if not isinstance(side, dict) or side.get("panel") != panel_name or "edge" not in side:
                continue
            edge_id = side["edge"]
            if removed_start <= edge_id < removed_end:
                side["edge"] = start_edge_id
            elif edge_id >= removed_end:
                side["edge"] += delta
        kept_stitches.append(stitch)

    spec["pattern"]["stitches"] = kept_stitches
    return warnings


def _stitches_for_edge(spec: SpecDict, panel_name: str, edge_id: int) -> List[Dict[str, Any]]:
    matches = []
    for stitch_id, stitch in enumerate(spec["pattern"].get("stitches", [])):
        if not isinstance(stitch, list) or len(stitch) != 2:
            continue
        for side_id, side in enumerate(stitch):
            if not isinstance(side, dict):
                continue
            if side.get("panel") == panel_name and side.get("edge") == edge_id:
                other_side = stitch[1 - side_id]
                if isinstance(other_side, dict) and "panel" in other_side and "edge" in other_side:
                    matches.append(
                        {
                            "stitch_id": stitch_id,
                            "other_panel": other_side["panel"],
                            "other_edge": int(other_side["edge"]),
                        }
                    )
                break
    return matches


def _split_straight_edge_at_fraction(panel: SpecDict, edge_id: int, fraction: float) -> Tuple[int, int]:
    edge = panel["edges"][edge_id]
    if "curvature" in edge:
        raise SemanticEditError("Cannot synchronize a dart insertion onto a curved stitched counterpart edge")
    fraction = max(1e-6, min(1.0 - 1e-6, float(fraction)))
    vertices = np.asarray(panel["vertices"], dtype=float)
    start_idx, end_idx = edge["endpoints"]
    split_point = vertices[start_idx] + (vertices[end_idx] - vertices[start_idx]) * fraction
    split_idx = len(panel["vertices"])
    panel["vertices"].append(_as_list(split_point))
    panel["edges"][edge_id : edge_id + 1] = [
        _copy_base_segment(edge, [start_idx, split_idx]),
        _copy_base_segment(edge, [split_idx, end_idx]),
    ]
    return edge_id, edge_id + 1


def _sync_stitched_counterpart_for_dart(
    spec: SpecDict,
    panel_name: str,
    replaced_edge_id: int,
    replacement_count: int,
    seam_stitches: List[Dict[str, Any]],
    dart_segment_edge_ids: Tuple[int, int],
    segment_lengths: Tuple[float, float],
) -> List[str]:
    """Split stitched counterpart edges so seam stitches stay one-to-one."""
    if not seam_stitches:
        return _remap_stitches_after_edge_replacement(spec, panel_name, replaced_edge_id, replacement_count)

    warnings = []
    base_delta = replacement_count - 1
    first_len, second_len = segment_lengths
    total_len = first_len + second_len
    split_fraction = first_len / total_len if total_len > 1e-8 else 0.5
    stitch_ids_to_replace = {item["stitch_id"] for item in seam_stitches}
    split_records = {}

    panels = spec["pattern"]["panels"]
    for item in sorted(seam_stitches, key=lambda value: (value["other_panel"], value["other_edge"]), reverse=True):
        other_panel_name = item["other_panel"]
        other_edge_id = item["other_edge"]
        if other_panel_name == panel_name:
            warnings.append(
                f"skipped automatic counterpart split for self-stitched edge {panel_name}.{other_edge_id}"
            )
            continue
        other_panel = panels.get(other_panel_name)
        if other_panel is None:
            warnings.append(f"missing stitched counterpart panel {other_panel_name}")
            continue
        first_edge_id, second_edge_id = _split_straight_edge_at_fraction(other_panel, other_edge_id, split_fraction)
        split_records[(other_panel_name, other_edge_id)] = (first_edge_id, second_edge_id)
        warnings.append(
            f"split stitched counterpart {other_panel_name}.{other_edge_id} for dart insertion on {panel_name}.{replaced_edge_id}"
        )

    kept_stitches = []
    for stitch_id, stitch in enumerate(spec["pattern"].get("stitches", [])):
        if stitch_id in stitch_ids_to_replace:
            continue
        if not isinstance(stitch, list):
            kept_stitches.append(stitch)
            continue
        for side in stitch:
            if not isinstance(side, dict) or "panel" not in side or "edge" not in side:
                continue
            side_panel = side["panel"]
            side_edge = int(side["edge"])
            if side_panel == panel_name:
                if side_edge == replaced_edge_id:
                    side["edge"] = dart_segment_edge_ids[0]
                    warnings.append(
                        f"stitch {stitch_id} on {panel_name}.{replaced_edge_id} was mapped to the first dart seam segment"
                    )
                elif side_edge > replaced_edge_id:
                    side["edge"] = side_edge + base_delta
                continue
            for other_panel_name, other_edge_id in sorted(split_records.keys(), reverse=True):
                if side_panel != other_panel_name:
                    continue
                if side_edge == other_edge_id:
                    side["edge"] = split_records[(other_panel_name, other_edge_id)][0]
                elif side_edge > other_edge_id:
                    side["edge"] = side_edge + 1
        kept_stitches.append(stitch)

    for item in seam_stitches:
        split_pair = split_records.get((item["other_panel"], item["other_edge"]))
        if split_pair is None:
            continue
        kept_stitches.append(
            [
                {"panel": panel_name, "edge": dart_segment_edge_ids[0]},
                {"panel": item["other_panel"], "edge": split_pair[0]},
            ]
        )
        kept_stitches.append(
            [
                {"panel": panel_name, "edge": dart_segment_edge_ids[1]},
                {"panel": item["other_panel"], "edge": split_pair[1]},
            ]
        )

    spec["pattern"]["stitches"] = kept_stitches
    return warnings


def _dart_leg_pairs(panel: SpecDict) -> List[Tuple[int, int, Optional[str], int, int, int]]:
    """Return consecutive dart-leg pairs as edge ids plus mouth/apex vertices."""
    pairs = []
    edges = panel.get("edges", [])
    for edge_id in range(len(edges) - 1):
        first = edges[edge_id]
        second = edges[edge_id + 1]
        if _edge_semantic_label(first) != "Dart" or _edge_semantic_label(second) != "Dart":
            continue

        first_endpoints = first.get("endpoints", [])
        second_endpoints = second.get("endpoints", [])
        if len(first_endpoints) != 2 or len(second_endpoints) != 2:
            continue

        shared = set(first_endpoints) & set(second_endpoints)
        if len(shared) != 1:
            continue

        apex_idx = next(iter(shared))
        mouth_a = first_endpoints[0] if first_endpoints[1] == apex_idx else first_endpoints[1]
        mouth_b = second_endpoints[0] if second_endpoints[1] == apex_idx else second_endpoints[1]
        dart_id = first.get("dart_id") if first.get("dart_id") == second.get("dart_id") else None
        pairs.append((edge_id, edge_id + 1, dart_id, mouth_a, apex_idx, mouth_b))
    return pairs


def _contains_english_word(text_lower: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text_lower) is not None


def _target_panels_from_text(text: str, default_all: bool = True) -> List[str]:
    text_lower = text.lower()
    front = "前片" in text or _contains_english_word(text_lower, "front")
    back = "后片" in text or _contains_english_word(text_lower, "back")
    left = "左" in text or _contains_english_word(text_lower, "left")
    right = "右" in text or (
        _contains_english_word(text_lower, "right")
        and not re.search(r"\bright\s*[- ]?\s*(?:angle|angled)\b", text_lower)
    )

    if left and right:
        if front and not back:
            return ["LFP", "RFP"]
        if back and not front:
            return ["LBP", "RBP"]
        return ["LFP", "RFP", "LBP", "RBP"]
    if left and front:
        return ["LFP"]
    if right and front:
        return ["RFP"]
    if left and back:
        return ["LBP"]
    if right and back:
        return ["RBP"]
    if front:
        return ["LFP", "RFP"]
    if back:
        return ["LBP", "RBP"]
    if left:
        return ["LFP", "LBP"]
    if right:
        return ["RFP", "RBP"]
    if default_all:
        return ["LFP", "RFP", "LBP", "RBP"]
    return []


def _edge_label_from_text_fragment(fragment: str) -> Optional[str]:
    fragment_lower = re.sub(r"[\s_-]+", "", fragment.lower())
    if "下摆" in fragment or "hem" in fragment_lower:
        return "HEL"
    if "侧缝" in fragment or "sideedge" in fragment_lower or "sideseam" in fragment_lower:
        return "SS"
    if "肩线" in fragment or "肩边" in fragment or "shoulderseam" in fragment_lower or "shoulderedge" in fragment_lower or "shoulderline" in fragment_lower or "shoulder" in fragment_lower:
        return "SH"
    if "袖窿" in fragment or "袖笼" in fragment or "armhole" in fragment_lower or "armscye" in fragment_lower:
        return "AH"
    if "领口" in fragment or "neck" in fragment_lower:
        return "NECK"
    if "前中" in fragment or "centerfront" in fragment_lower or "centrefront" in fragment_lower:
        return "CF"
    if "后中" in fragment or "centerback" in fragment_lower or "centreback" in fragment_lower:
        return "CB"
    return None


def _dart_edges_from_text(text: str) -> Tuple[str, str, bool, bool]:
    text_lower = text.lower()
    base_edge = "SS"
    reference_edge = "HEL"
    base_mentioned = False
    reference_mentioned = False

    on_match = re.search(
        r"\b(?:on|at|along)\s+(?:the\s+)?(.+?)(?:,|\s+\d|\s+with|\s+from|\s+in\s+(?:from|front)|$)",
        text_lower,
    )
    if on_match:
        parsed = _edge_label_from_text_fragment(on_match.group(1))
        if parsed:
            base_edge = parsed
            base_mentioned = True
    chinese_on_match = re.search(r"在(.+?)(?:距|插入|加|做)", text)
    if chinese_on_match:
        parsed = _edge_label_from_text_fragment(chinese_on_match.group(1))
        if parsed:
            base_edge = parsed
            base_mentioned = True

    if not base_mentioned:
        base_text = re.sub(r"\b(?:from|in\s+from|in\s+front)\b.+?(?=,|\bwith\b|$)", "", text_lower)
        base_text = re.sub(r"距.+?(?=\d|[0-9]|处|，|,|的|$)", "", base_text)
        parsed = _edge_label_from_text_fragment(base_text)
        if parsed:
            base_edge = parsed
            base_mentioned = True

    from_match = re.search(
        r"\b(?:from|in\s+from|in\s+front(?:\s+of)?)\s+(?:the\s+)?(.+?)(?:,|\s+with|$)",
        text_lower,
    )
    if from_match:
        parsed = _edge_label_from_text_fragment(from_match.group(1))
        if parsed:
            reference_edge = parsed
            reference_mentioned = True
    chinese_from_match = re.search(r"距(.+?)(?:\d|[0-9]|处|，|,|的|$)", text)
    if chinese_from_match:
        parsed = _edge_label_from_text_fragment(chinese_from_match.group(1))
        if parsed:
            reference_edge = parsed
            reference_mentioned = True

    return base_edge, reference_edge, base_mentioned, reference_mentioned


def _first_number_match(patterns: Sequence[str], text: str, flags: int = re.IGNORECASE) -> Optional[float]:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return float(match.group(1))
    return None


def _dart_measurements_from_text(text: str) -> Tuple[float, float, float]:
    number = r"(\d+(?:\.\d+)?)"
    unit = r"\s*(?:cm|厘米|公分)?"
    distance = _first_number_match(
        [
            rf"{number}{unit}\s*(?:away\s+)?(?:from|in\s+from|in\s+front(?:\s+of)?)\b",
            rf"(?:distance|offset)\s*(?:from\s+.+?)?\s*(?:=|:|is|of)?\s*{number}{unit}",
            rf"(?:距|离).{{0,12}}?{number}{unit}",
        ],
        text,
    )
    width = _first_number_match(
        [
            rf"{number}{unit}\s*[- ]*\s*(?:wide|width)\b",
            rf"(?:width|wide)\s*(?:=|:|is|of)?\s*{number}{unit}",
            rf"宽\s*{number}{unit}",
        ],
        text,
    )
    depth = _first_number_match(
        [
            rf"{number}{unit}\s*[- ]*\s*(?:deep|depth|long|length)\b",
            rf"(?:depth|deep|length|long)\s*(?:=|:|is|of)?\s*{number}{unit}",
            rf"深\s*{number}{unit}",
        ],
        text,
    )
    numbers = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*(?:cm|厘米|公分)?", text)]
    if distance is None:
        distance = numbers[0] if len(numbers) > 0 else 12.0
    if width is None:
        width = numbers[1] if len(numbers) > 1 else 2.0
    if depth is None:
        depth = numbers[2] if len(numbers) > 2 else 10.0
    return distance, width, depth


def _dart_index_from_text(text: str) -> Optional[int]:
    text_lower = text.lower()
    ordinal_words = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
        "sixth": 6,
        "seventh": 7,
        "eighth": 8,
        "ninth": 9,
        "tenth": 10,
    }
    for word, value in ordinal_words.items():
        if re.search(rf"\b{word}\b.*\bdart\b|\bdart\b.*\b{word}\b", text_lower):
            return value

    digit_patterns = [
        r"\b(?:dart|省道)\s*(?:#|number|no\.?)?\s*(\d+)\b",
        r"\b(\d+)(?:st|nd|rd|th)\s+(?:dart)\b",
        r"\b(?:the\s+)?(\d+)(?:st|nd|rd|th)\b.*\bdart\b",
        r"第\s*(\d+)\s*(?:个|道)?\s*省道",
    ]
    for pattern in digit_patterns:
        match = re.search(pattern, text_lower)
        if match:
            return int(match.group(1))

    chinese_ordinals = {
        "第一个": 1,
        "第一道": 1,
        "第二个": 2,
        "第二道": 2,
        "第三个": 3,
        "第三道": 3,
        "第四个": 4,
        "第四道": 4,
        "第五个": 5,
        "第五道": 5,
    }
    for token, value in chinese_ordinals.items():
        if token in text and "省道" in text:
            return value
    return None


def _component_side_from_text(text: str) -> str:
    text_lower = text.lower()
    if any(token in text for token in ["所有", "全部", "两只", "双侧"]) or any(
        token in text_lower for token in ["all", "both"]
    ):
        return "all"
    if ("左" in text and "右" in text) or (
        _contains_english_word(text_lower, "left") and _contains_english_word(text_lower, "right")
    ):
        return "all"
    if "左" in text or "left" in text_lower:
        return "left"
    if "右" in text or "right" in text_lower:
        return "right"
    return "all"


def _component_panel_names(spec: SpecDict, component: str, side: str = "all") -> List[str]:
    if component != "sleeve":
        return []

    def is_side_match(panel_name: str) -> bool:
        lower = panel_name.lower()
        if side in {"all", "both"}:
            return True
        if side == "left":
            return "left" in lower or lower.startswith("l_sleeve") or lower.startswith("lsleeve")
        if side == "right":
            return "right" in lower or lower.startswith("r_sleeve") or lower.startswith("rsleeve")
        return False

    matches = []
    for panel_name, panel in spec["pattern"]["panels"].items():
        lower_name = panel_name.lower()
        label = str(panel.get("label", "")).lower()
        prediction = str(_prediction_code(panel) or "").lower()
        semantic = str(_panel_semantic_label(panel) or "").lower()
        is_sleeve = (
            "sleeve" in lower_name
            or "sleeve" in label
            or "sleeve" in prediction
            or "sleeve" in semantic
            or "袖" in panel_name
        )
        if is_sleeve and is_side_match(panel_name):
            matches.append(panel_name)
    return matches


def parse_semantic_command(text: str) -> SpecDict:
    """Parse the small natural-language surface used by the GUI.

    JSON commands are accepted directly. Chinese free text currently maps the
    requested acceptance phrases to the structured command schema.
    """

    cleaned = text.strip()
    if not cleaned:
        raise SemanticEditError("Please enter a semantic edit command")

    if cleaned.startswith("{"):
        try:
            command = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise SemanticEditError(f"Invalid command JSON: {exc}") from exc
        if not isinstance(command, dict):
            raise SemanticEditError("Command JSON must be an object")
        return command

    text_lower = cleaned.lower()
    command_lower = re.sub(r"[\s_-]+", "", text_lower)
    angle_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|度|degrees?|deg)?", cleaned, re.IGNORECASE)
    requested_angle = float(angle_match.group(1)) if angle_match else 90.0

    remove_tokens = ["去掉", "删除", "移除", "remove", "delete", "drop", "erase"]
    if (
        any(token in cleaned or token in text_lower for token in remove_tokens)
        or "sleeveless" in command_lower
        or "无袖" in cleaned
    ) and (
        "袖" in cleaned or "sleeve" in command_lower
    ):
        return {
            "operation": "remove_component",
            "component": "sleeve",
            "side": _component_side_from_text(cleaned),
        }

    if ("袖窿" in cleaned or "袖笼" in cleaned or "armhole" in command_lower) and (
        "肩线" in cleaned or "肩边" in cleaned or "shoulder" in command_lower
    ):
        return {
            "operation": "enforce_curve_endpoint_angle",
            "target_panels": _target_panels_from_text(cleaned),
            "line_edge": "SH",
            "curve_edge": "AH",
            "angle_deg": requested_angle,
            "mode": "adjust_curve_tangent",
        }

    if ("袖窿" in cleaned or "袖笼" in cleaned or "armhole" in command_lower) and (
        "侧缝" in cleaned or "side seam" in text_lower or "sideseam" in command_lower
    ):
        return {
            "operation": "enforce_curve_endpoint_angle",
            "target_panels": _target_panels_from_text(cleaned),
            "line_edge": "SS",
            "curve_edge": "AH",
            "angle_deg": requested_angle,
            "mode": "adjust_curve_tangent",
        }

    if "省道" in cleaned or "dart" in text_lower:
        numbers = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*(?:cm|厘米|公分)?", cleaned)]
        distance, width, depth = _dart_measurements_from_text(cleaned)
        base_edge, reference_edge, base_mentioned, _reference_mentioned = _dart_edges_from_text(cleaned)
        if any(token in cleaned or token in text_lower for token in remove_tokens):
            dart_index = _dart_index_from_text(cleaned)
            command = {
                "operation": "remove_edge_dart",
                "target_panels": _target_panels_from_text(cleaned),
                "base_edge": base_edge if base_mentioned else "ANY",
                "reference_edge": reference_edge,
            }
            if dart_index is not None:
                command["dart_index"] = dart_index
            elif numbers:
                command["distance_from_reference_cm"] = distance
            return command
        return {
            "operation": "insert_edge_dart",
            "target_panels": _target_panels_from_text(cleaned),
            "base_edge": base_edge,
            "reference_edge": reference_edge,
            "distance_from_reference_cm": distance,
            "dart_width_cm": width,
            "dart_depth_cm": depth,
            "mirror": True,
            "add_dart_stitch": True,
        }

    raise SemanticEditError("Unsupported semantic command. Try one of the built-in examples or paste a JSON command.")


class SemanticPatternEditor:
    """Stateful editor used by the GUI panel."""

    def __init__(self, work_dir: Optional[Path] = None) -> None:
        self.work_dir = Path(work_dir or (Path.cwd() / "tmp_gui" / "semantic_editor"))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.original_spec: Optional[SpecDict] = None
        self.current_spec: Optional[SpecDict] = None
        self.upload_name: str = ""
        self.report: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.last_error: str = ""
        self._last_render_files: List[Path] = []

    def release(self) -> None:
        for path in self._last_render_files:
            path.unlink(missing_ok=True)
        self._last_render_files.clear()

    @property
    def has_pattern(self) -> bool:
        return self.current_spec is not None

    def load_bytes(self, data: bytes, name: str = "uploaded_pattern.json") -> None:
        spec = _read_json_bytes(data)
        normalize_semantic_predictions(spec)
        self.original_spec = copy.deepcopy(spec)
        self.current_spec = copy.deepcopy(spec)
        self.upload_name = name
        self.report = []
        self.warnings = []
        self.last_error = ""

    def reset_to_original(self) -> None:
        if self.original_spec is None:
            raise SemanticEditError("No semantic pattern has been uploaded yet")
        self.current_spec = copy.deepcopy(self.original_spec)
        self.report = []
        self.warnings = []
        self.last_error = ""

    def apply_text_command(self, text: str) -> EditResult:
        return self.apply_command(parse_semantic_command(text))

    def apply_command(self, command: SpecDict) -> EditResult:
        if self.current_spec is None:
            raise SemanticEditError("Upload a semantic pattern JSON before applying edits")

        operation = command.get("operation")
        spec = copy.deepcopy(self.current_spec)
        if operation == "enforce_curve_endpoint_angle":
            result = self._enforce_curve_endpoint_angle(spec, command)
        elif operation == "insert_edge_dart":
            result = self._insert_edge_dart(spec, command)
        elif operation == "remove_edge_dart":
            result = self._remove_edge_dart(spec, command)
        elif operation == "remove_component":
            result = self._remove_component(spec, command)
        elif operation == "add_patch_pocket":
            result = self._add_patch_pocket(spec, command)
        else:
            raise SemanticEditError(f"Unsupported operation: {operation}")

        self.current_spec = result.spec
        self.report = result.report
        self.warnings = result.warnings
        self.last_error = ""
        return result

    def _enforce_curve_endpoint_angle(self, spec: SpecDict, command: SpecDict) -> EditResult:
        line_label = command.get("line_edge")
        curve_label = command.get("curve_edge")
        if line_label not in EDGE_LABELS or curve_label not in EDGE_LABELS:
            raise SemanticEditError("line_edge and curve_edge must be known edge semantic labels")
        if command.get("mode", "adjust_curve_tangent") != "adjust_curve_tangent":
            raise SemanticEditError("Only mode=adjust_curve_tangent is supported")

        target_panels = command.get("target_panels") or ["LFP", "RFP"]
        angle_deg = float(command.get("angle_deg", 90.0))
        report = []

        for panel_name, panel in _iter_target_panels(spec, target_panels):
            line_edge_id, curve_edge_id, shared_vertex = _find_shared_edge_pair(
                panel, panel_name, line_label, curve_label
            )
            vertices = np.asarray(panel["vertices"], dtype=float)
            line_edge = panel["edges"][line_edge_id]
            line_other = line_edge["endpoints"][0]
            if line_other == shared_vertex:
                line_other = line_edge["endpoints"][1]
            line_vec = vertices[line_other] - vertices[shared_vertex]
            line_unit = _unit(line_vec, f"{panel_name}.{line_label}")

            old_tangent, cp_index, curve_style = _curve_tangent_at_shared(
                spec, panel, curve_edge_id, shared_vertex
            )
            old_len = float(np.linalg.norm(old_tangent))
            if old_len < 1e-8:
                curve_edge = panel["edges"][curve_edge_id]
                curve_other = curve_edge["endpoints"][0]
                if curve_other == shared_vertex:
                    curve_other = curve_edge["endpoints"][1]
                old_tangent = vertices[curve_other] - vertices[shared_vertex]
                old_len = float(np.linalg.norm(old_tangent))
            if old_len < 1e-8:
                raise SemanticEditError(f"{panel_name}: curve tangent length is zero")

            candidate_a = _rotate(line_unit, angle_deg) * old_len
            candidate_b = _rotate(line_unit, -angle_deg) * old_len
            new_tangent = candidate_a if np.linalg.norm(candidate_a - old_tangent) <= np.linalg.norm(candidate_b - old_tangent) else candidate_b
            connector_result = _smooth_short_armhole_connector(
                spec,
                panel,
                curve_edge_id,
                curve_label,
                shared_vertex,
                new_tangent,
                float(command.get("short_connector_threshold_cm", 1.5)),
            )
            moved_connector_vertex = connector_result["moved_connector_vertex"]
            if moved_connector_vertex is not None:
                vertices = np.asarray(panel["vertices"], dtype=float)
            shared = vertices[shared_vertex]
            changed = _write_curve_control(
                spec,
                panel,
                curve_edge_id,
                shared_vertex,
                shared + new_tangent,
                cp_index,
            )
            after_tangent, _, _ = _curve_tangent_at_shared(spec, panel, curve_edge_id, shared_vertex)
            before_angle = _angle_deg(line_vec, old_tangent)
            after_angle = _angle_deg(line_vec, after_tangent)

            report.append(
                {
                    "operation": "enforce_curve_endpoint_angle",
                    "panel": panel_name,
                    "panel_label": _panel_display_label(panel_name, panel),
                    "line_edge": line_label,
                    "line_edge_id": line_edge_id,
                    "curve_edge": curve_label,
                    "curve_edge_id": curve_edge_id,
                    "shared_vertex": int(shared_vertex),
                    "before_angle_deg": before_angle,
                    "after_angle_deg": after_angle,
                    "target_angle_deg": angle_deg,
                    "changed": changed,
                    "curve_style": curve_style,
                    "moved_connector_vertex": moved_connector_vertex,
                    "smoothed_curve_edge_id": connector_result["smoothed_curve_edge_id"],
                }
            )

        return EditResult(spec=spec, report=report)

    def _insert_edge_dart(self, spec: SpecDict, command: SpecDict) -> EditResult:
        target_panels = command.get("target_panels") or ["LFP", "RFP"]
        base_label = command.get("base_edge", "SS")
        reference_label = command.get("reference_edge", "HEL")
        if base_label not in EDGE_LABELS or reference_label not in EDGE_LABELS:
            raise SemanticEditError("base_edge and reference_edge must be known edge semantic labels")

        units_per_cm = _pattern_units_per_cm(spec)
        distance_cm = float(command.get("distance_from_reference_cm", 12.0))
        width_cm = float(command.get("dart_width_cm", command.get("dart_depth_cm", 2.0)))
        if "dart_width_cm" not in command and "dart_length_cm" in command:
            # Backward compatibility with the earlier UI where "depth" meant
            # dart mouth width and "length" meant the perpendicular dart depth.
            depth_cm = float(command["dart_length_cm"])
        else:
            depth_cm = float(command.get("dart_depth_cm", 10.0))
        distance = distance_cm * units_per_cm
        width = width_cm * units_per_cm
        depth = depth_cm * units_per_cm
        add_dart_stitch = bool(command.get("add_dart_stitch", True))
        if width <= 0:
            raise SemanticEditError("dart_width_cm must be positive")
        if depth <= 0:
            raise SemanticEditError("dart_depth_cm must be positive")

        report = []
        warnings = []
        for panel_name, panel in _iter_target_panels(spec, target_panels):
            reference_edge_id = _find_reference_edge_for_base(panel, panel_name, reference_label, base_label)
            base_edge_id = _find_edge_near_reference(panel, panel_name, base_label, reference_edge_id)
            base_edge = panel["edges"][base_edge_id]
            seam_stitches = _stitches_for_edge(spec, panel_name, base_edge_id)
            reference_edge = panel["edges"][reference_edge_id]
            if "curvature" in base_edge:
                raise SemanticEditError(f"{panel_name}: dart insertion currently supports straight base edges only")

            vertices = np.asarray(panel["vertices"], dtype=float)
            start_idx, end_idx = base_edge["endpoints"]
            start = vertices[start_idx]
            end = vertices[end_idx]
            ref_start, ref_end = [vertices[idx] for idx in reference_edge["endpoints"]]
            dist_start = _distance_point_to_segment(start, ref_start, ref_end)
            dist_end = _distance_point_to_segment(end, ref_start, ref_end)
            if dist_start <= dist_end:
                s_idx, other_idx = start_idx, end_idx
                s_point, other_point = start, end
                reference_at_original_start = True
            else:
                s_idx, other_idx = end_idx, start_idx
                s_point, other_point = end, start
                reference_at_original_start = False

            base_vec = other_point - s_point
            base_len = float(np.linalg.norm(base_vec))
            if base_len < 1e-8:
                raise SemanticEditError(f"{panel_name}: base edge {base_label} is zero-length")
            if distance - width / 2 < -1e-8 or distance + width / 2 > base_len + 1e-8:
                raise SemanticEditError(
                    f"{panel_name}: dart mouth [{distance - width / 2:.2f}, {distance + width / 2:.2f}] "
                    f"falls outside {base_label} length {base_len:.2f}"
                )

            base_unit = base_vec / base_len
            center = s_point + base_unit * distance
            p1 = center - base_unit * (width / 2.0)
            p2 = center + base_unit * (width / 2.0)
            centroid = np.mean(vertices, axis=0)
            normal_a = np.asarray([-base_unit[1], base_unit[0]])
            normal_b = -normal_a
            inward = normal_a if np.dot(normal_a, centroid - center) >= np.dot(normal_b, centroid - center) else normal_b
            apex = center + inward * depth
            side_length = float(np.linalg.norm(apex - p1))

            p1_idx = len(panel["vertices"])
            apex_idx = p1_idx + 1
            p2_idx = p1_idx + 2
            panel["vertices"].extend([_as_list(p1), _as_list(apex), _as_list(p2)])

            dart_id = _next_dart_id(spec)
            if reference_at_original_start:
                replacement_edges = [
                    _copy_base_segment(base_edge, [s_idx, p1_idx]),
                    _dart_leg([p1_idx, apex_idx], dart_id, "leg_1"),
                    _dart_leg([apex_idx, p2_idx], dart_id, "leg_2"),
                    _copy_base_segment(base_edge, [p2_idx, other_idx]),
                ]
            else:
                replacement_edges = [
                    _copy_base_segment(base_edge, [other_idx, p2_idx]),
                    _dart_leg([p2_idx, apex_idx], dart_id, "leg_1"),
                    _dart_leg([apex_idx, p1_idx], dart_id, "leg_2"),
                    _copy_base_segment(base_edge, [p1_idx, s_idx]),
                ]

            panel["edges"][base_edge_id : base_edge_id + 1] = replacement_edges
            warnings.extend(
                _sync_stitched_counterpart_for_dart(
                    spec,
                    panel_name,
                    base_edge_id,
                    len(replacement_edges),
                    seam_stitches,
                    (base_edge_id, base_edge_id + 3),
                    (max(distance - width / 2.0, 0.0), max(base_len - distance - width / 2.0, 0.0)),
                )
            )
            dart_leg_1_edge_id = base_edge_id + 1
            dart_leg_2_edge_id = base_edge_id + 2
            if add_dart_stitch:
                spec["pattern"].setdefault("stitches", []).append(
                    [
                        {"panel": panel_name, "edge": dart_leg_1_edge_id},
                        {"panel": panel_name, "edge": dart_leg_2_edge_id},
                    ]
                )

            report.append(
                {
                    "operation": "insert_edge_dart",
                    "panel": panel_name,
                    "panel_label": _panel_display_label(panel_name, panel),
                    "base_edge": base_label,
                    "base_edge_id": base_edge_id,
                    "reference_edge": reference_label,
                    "reference_edge_id": reference_edge_id,
                    "dart_id": dart_id,
                    "dart_leg_1_edge_id": dart_leg_1_edge_id,
                    "dart_leg_2_edge_id": dart_leg_2_edge_id,
                    "dart_width_cm": width_cm,
                    "dart_depth_cm": depth_cm,
                    "dart_side_length_cm": side_length / units_per_cm,
                    "distance_from_reference_cm": distance_cm,
                    "add_dart_stitch": add_dart_stitch,
                }
            )

        return EditResult(spec=spec, report=report, warnings=warnings)

    def _remove_edge_dart(self, spec: SpecDict, command: SpecDict) -> EditResult:
        target_panels = command.get("target_panels") or ["LFP", "RFP"]
        base_label = command.get("base_edge", "SS")
        reference_label = command.get("reference_edge", "HEL")
        any_base_edge = str(base_label).upper() in {"ANY", "ANY EDGE", "*", ""}
        if (not any_base_edge and base_label not in EDGE_LABELS) or reference_label not in EDGE_LABELS:
            raise SemanticEditError("base_edge and reference_edge must be known edge semantic labels")

        units_per_cm = _pattern_units_per_cm(spec)
        target_distance = command.get("distance_from_reference_cm")
        target_distance_units = None if target_distance is None else float(target_distance) * units_per_cm
        target_dart_index = command.get("dart_index")
        if target_dart_index is not None:
            target_dart_index = int(target_dart_index)
            if target_dart_index < 1:
                raise SemanticEditError("dart_index must be 1 or greater")
        report = []
        warnings = []

        for panel_name, panel in _iter_target_panels(spec, target_panels):
            vertices = np.asarray(panel["vertices"], dtype=float)
            dart_pairs = _dart_leg_pairs(panel)
            if not dart_pairs:
                warnings.append(f"{panel_name}: no removable dart found")
                continue

            reference_edge_ids = _candidate_edge_ids(panel, reference_label)
            if not reference_edge_ids:
                raise SemanticEditError(f"{panel_name}: no edge with semantic_label={reference_label}")
            reference_segments = [
                tuple(vertices[idx] for idx in panel["edges"][reference_edge_id]["endpoints"])
                for reference_edge_id in reference_edge_ids
            ]

            def min_reference_distance(point: np.ndarray) -> float:
                return min(
                    _distance_point_to_segment(point, ref_start, ref_end)
                    for ref_start, ref_end in reference_segments
                )

            candidates = []
            for leg_1_edge_id, leg_2_edge_id, dart_id, mouth_a_idx, apex_idx, mouth_b_idx in dart_pairs:
                prev_edge_id = leg_1_edge_id - 1
                next_edge_id = leg_2_edge_id + 1
                if prev_edge_id < 0 or next_edge_id >= len(panel["edges"]):
                    continue
                prev_edge = panel["edges"][prev_edge_id]
                next_edge = panel["edges"][next_edge_id]
                prev_label = _edge_semantic_label(prev_edge)
                next_label = _edge_semantic_label(next_edge)
                if any_base_edge:
                    if prev_label != next_label or prev_label == "Dart" or prev_label is None:
                        continue
                    candidate_base_label = prev_label
                elif prev_label == base_label and next_label == base_label:
                    candidate_base_label = base_label
                else:
                    continue

                mouth_a = vertices[mouth_a_idx]
                mouth_b = vertices[mouth_b_idx]
                center = (mouth_a + mouth_b) / 2.0
                prev_base_idx = prev_edge["endpoints"][0] if prev_edge["endpoints"][1] in {mouth_a_idx, mouth_b_idx} else prev_edge["endpoints"][1]
                next_base_idx = next_edge["endpoints"][0] if next_edge["endpoints"][1] in {mouth_a_idx, mouth_b_idx} else next_edge["endpoints"][1]
                prev_base = vertices[prev_base_idx]
                next_base = vertices[next_base_idx]
                prev_ref_dist = min_reference_distance(prev_base)
                next_ref_dist = min_reference_distance(next_base)
                reference_base = prev_base if prev_ref_dist <= next_ref_dist else next_base
                distance_from_reference = float(np.linalg.norm(center - reference_base))
                score = abs(distance_from_reference - target_distance_units) if target_distance_units is not None else distance_from_reference
                candidates.append(
                    (
                        score,
                        prev_edge_id,
                        leg_1_edge_id,
                        leg_2_edge_id,
                        next_edge_id,
                        dart_id,
                        prev_base_idx,
                        next_base_idx,
                        mouth_a_idx,
                        apex_idx,
                        mouth_b_idx,
                        distance_from_reference,
                        candidate_base_label,
                    )
                )

            if not candidates:
                warnings.append(f"{panel_name}: no {base_label} dart could be removed")
                continue

            if target_dart_index is not None:
                ordered_candidates = sorted(candidates, key=lambda item: (item[1], item[2]))
                if target_dart_index > len(ordered_candidates):
                    warnings.append(
                        f"{panel_name}: only {len(ordered_candidates)} removable {base_label} dart(s) found"
                    )
                    continue
                selected_candidate = ordered_candidates[target_dart_index - 1]
            else:
                selected_candidate = sorted(candidates, key=lambda item: item[0])[0]

            (
                _score,
                prev_edge_id,
                leg_1_edge_id,
                leg_2_edge_id,
                next_edge_id,
                dart_id,
                prev_base_idx,
                next_base_idx,
                mouth_a_idx,
                apex_idx,
                mouth_b_idx,
                distance_from_reference,
                candidate_base_label,
            ) = selected_candidate

            restored_edge = _copy_base_segment(panel["edges"][prev_edge_id], [prev_base_idx, next_base_idx])
            restored_edge["semantic_label"] = candidate_base_label
            old_edge_count = next_edge_id - prev_edge_id + 1
            panel["edges"][prev_edge_id : next_edge_id + 1] = [restored_edge]
            warnings.extend(
                _remap_stitches_after_dart_removal(
                    spec,
                    panel_name,
                    prev_edge_id,
                    old_edge_count,
                    1,
                )
            )

            width = float(np.linalg.norm(vertices[mouth_b_idx] - vertices[mouth_a_idx]))
            depth = _distance_point_to_segment(vertices[apex_idx], vertices[mouth_a_idx], vertices[mouth_b_idx])
            report.append(
                {
                    "operation": "remove_edge_dart",
                    "panel": panel_name,
                    "panel_label": _panel_display_label(panel_name, panel),
                    "base_edge": candidate_base_label,
                    "base_edge_id": prev_edge_id,
                    "reference_edge": reference_label,
                    "reference_edge_id": reference_edge_ids,
                    "dart_id": dart_id or "unlabeled",
                    "removed_dart_leg_edge_ids": [leg_1_edge_id, leg_2_edge_id],
                    "dart_width_cm": width / units_per_cm,
                    "dart_depth_cm": depth / units_per_cm,
                    "distance_from_reference_cm": distance_from_reference / units_per_cm,
                    "dart_index": target_dart_index,
                }
            )

        if not report:
            raise SemanticEditError("; ".join(warnings) or "No removable dart found")
        return EditResult(spec=spec, report=report, warnings=warnings)

    def _remove_component(self, spec: SpecDict, command: SpecDict) -> EditResult:
        component = str(command.get("component", "")).lower()
        side = str(command.get("side", "all")).lower()
        if component not in {"sleeve", "sleeves"}:
            raise SemanticEditError("remove_component currently supports component=sleeve")
        if side not in {"left", "right", "all", "both"}:
            raise SemanticEditError("remove_component side must be left, right, all, or both")

        panels = spec["pattern"]["panels"]
        removed_panel_names = _component_panel_names(spec, "sleeve", "all" if side == "both" else side)
        if not removed_panel_names:
            raise SemanticEditError(f"No {side} sleeve panels were found")

        removed_set = set(removed_panel_names)
        for panel_name in removed_panel_names:
            panels.pop(panel_name, None)

        panel_order = spec["pattern"].get("panel_order")
        if isinstance(panel_order, list):
            spec["pattern"]["panel_order"] = [name for name in panel_order if name not in removed_set]

        kept_stitches = []
        removed_stitch_count = 0
        for stitch in spec["pattern"].get("stitches", []):
            if isinstance(stitch, list) and any(
                isinstance(side_info, dict) and side_info.get("panel") in removed_set
                for side_info in stitch
            ):
                removed_stitch_count += 1
                continue
            kept_stitches.append(stitch)
        spec["pattern"]["stitches"] = kept_stitches

        return EditResult(
            spec=spec,
            report=[
                {
                    "operation": "remove_component",
                    "component": "sleeve",
                    "side": "all" if side == "both" else side,
                    "removed_panels": removed_panel_names,
                    "removed_panel_count": len(removed_panel_names),
                    "removed_stitch_count": removed_stitch_count,
                }
            ],
        )

    def _panel_for_pocket(self, spec, side, position):
        panels = spec['pattern']['panels']
        is_back = position == "back"

        if is_back:
            for name in [f"pant_b_{side[0]}", f"{side[0].upper()}BP", f"{side}_back"]:
                if name in panels:
                    return name, panels[name]

        for name in [f"pant_f_{side[0]}", f"{side[0].upper()}FP", f"{side}_front"]:
            if name in panels:
                return name, panels[name]

        for name, panel in panels.items():
            if is_back and "back" in name.lower():
                return name, panel
            if not is_back and "front" in name.lower():
                return name, panel

        for name, panel in panels.items():
            if "pocket" not in name.lower():
                return name, panel

        raise SemanticEditError("No panel was found for pocket placement")

    def _add_patch_pocket(self, spec: SpecDict, command: SpecDict) -> EditResult:
        units_per_cm = _pattern_units_per_cm(spec)
        side = str(command.get("side", "left")).lower()
        if side not in {"left", "right"}:
            raise SemanticEditError("add_patch_pocket side must be left or right")

        position = str(command.get("position", "front")).lower()
        if position not in {"front", "back"}:
            raise SemanticEditError("add_patch_pocket position must be front or back")
        is_back = position == "back"

        target_panel_name, target_panel = self._panel_for_pocket(spec, side, position)
        width = float(command.get("pocket_width_cm", command.get("size_cm", 12.0))) * units_per_cm
        height = float(command.get("pocket_height_cm", command.get("size_cm", 12.0))) * units_per_cm

        if is_back:
            center_offset = float(command.get("distance_from_center_back_cm", 8.0)) * units_per_cm
            waist_offset = float(command.get("distance_from_waist_cm", 12.0)) * units_per_cm
        else:
            center_offset = float(command.get("distance_from_center_front_cm", 8.0)) * units_per_cm
            waist_offset = float(command.get("distance_from_hem_cm", 10.0)) * units_per_cm

        surface_offset_cm = float(command.get("surface_offset_cm", 1.5))
        if width <= 0 or height <= 0:
            raise SemanticEditError("pocket_width_cm and pocket_height_cm must be positive")

        target_vertices_raw = target_panel.get("vertices", [])
        if not isinstance(target_vertices_raw, (list, tuple)):
            raise SemanticEditError(f"Target panel vertices must be a list, got {type(target_vertices_raw).__name__}")
        target_vertices = np.asarray(target_vertices_raw, dtype=float)
        x_min = float(target_vertices[:, 0].min())
        x_max = float(target_vertices[:, 0].max())
        y_min = float(target_vertices[:, 1].min())
        y_max = float(target_vertices[:, 1].max())

        if is_back:
            pocket_x = x_min + center_offset + width / 2.0
            pocket_y = y_max - waist_offset - height / 2.0
        else:
            pocket_x = x_min + center_offset + width / 2.0
            pocket_y = y_min + waist_offset + height / 2.0

        pocket_name = f"pocket_patch_{side[0]}_{'b' if is_back else 'f'}"
        idx = 1
        while pocket_name in spec["pattern"]["panels"]:
            pocket_name = f"pocket_patch_{side[0]}_{'b' if is_back else 'f'}_{idx}"
            idx += 1

        pocket_vertices_local = [
            [0.0, 0.0],
            [width, 0.0],
            [width, height],
            [0.0, height],
        ]
        pocket_vertices_list = [[float(v[0]), float(v[1])] for v in pocket_vertices_local]

        pocket_edges = [
            {"endpoints": [0, 1]},
            {"endpoints": [1, 2]},
            {"endpoints": [2, 3]},
            {"endpoints": [3, 0]},
        ]

        target_rotation = target_panel.get("rotation", [0.0, 0.0, 0.0])
        if isinstance(target_rotation, (int, float)):
            target_rotation = [float(target_rotation), 0.0, 0.0]
        elif not isinstance(target_rotation, (list, tuple)):
            target_rotation = [0.0, 0.0, 0.0]
        pocket_rotation = [float(x) for x in target_rotation]

        target_translation_raw = target_panel.get("translation", [0.0, 0.0, 0.0])
        if isinstance(target_translation_raw, (int, float)):
            target_translation = np.array([float(target_translation_raw), 0.0, 0.0], dtype=float)
        elif not isinstance(target_translation_raw, (list, tuple, np.ndarray)):
            target_translation = np.array([0.0, 0.0, 0.0], dtype=float)
        else:
            target_translation = np.asarray(target_translation_raw, dtype=float)
        R_target = rotation_tools.euler_xyz_to_R(
            np.asarray(pocket_rotation, dtype=float)
        )
        target_normal = R_target @ np.array([0.0, 0.0, 1.0])
        pocket_translation = (target_translation + target_normal * surface_offset_cm).tolist()
        pocket_translation[0] += pocket_x - width / 2.0
        pocket_translation[1] += pocket_y - height / 2.0

        pocket_panel = {
            "vertices": pocket_vertices_list,
            "edges": pocket_edges,
            "rotation": pocket_rotation,
            "translation": pocket_translation,
            "prediction": target_panel.get("prediction", []),
        }
        spec["pattern"]["panels"][pocket_name] = pocket_panel

        panel_order = spec["pattern"].get("panel_order")
        if isinstance(panel_order, list):
            panel_order.append(pocket_name)

        sewn_edges = [0, 1, 3]
        surface_stitches = spec["pattern"].setdefault("surface_stitches", [])

        for edge_id in sewn_edges:
            if edge_id == 0:
                start_local = [0.0, 0.0]
                end_local = [width, 0.0]
            elif edge_id == 1:
                start_local = [width, 0.0]
                end_local = [width, height]
            elif edge_id == 3:
                start_local = [0.0, height]
                end_local = [0.0, 0.0]
            else:
                continue

            def pocket_point_to_target_2d(point_2d):
                point_3d = np.array([point_2d[0], point_2d[1], 0.0])
                R_pocket = rotation_tools.euler_xyz_to_R(np.asarray(pocket_rotation, dtype=float))
                R_target_mat = rotation_tools.euler_xyz_to_R(np.asarray(target_rotation, dtype=float))

                world_point = R_pocket @ point_3d + np.asarray(pocket_translation, dtype=float)
                target_local = R_target_mat.T @ (world_point - np.asarray(target_translation, dtype=float))
                return target_local[:2].tolist()

            start = pocket_point_to_target_2d(start_local)
            end = pocket_point_to_target_2d(end_local)

            surface_stitches.append({
                "source": {
                    "panel": pocket_name,
                    "edge": edge_id,
                },
                "target": {
                    "panel": target_panel_name,
                    "segment": [
                        [float(start[0]), float(start[1])],
                        [float(end[0]), float(end[1])],
                    ],
                    "coordinate_space": "target_panel_2d",
                },
                "type": "patch_pocket_surface_stitch",
            })

        return EditResult(
            spec=spec,
            report=[
                {
                    "operation": "add_patch_pocket",
                    "position": position,
                    "side": side,
                    "target_panel": target_panel_name,
                    "pocket_panel": pocket_name,
                    "pocket_width_cm": width / units_per_cm,
                    "pocket_height_cm": height / units_per_cm,
                    **({"distance_from_center_back_cm": center_offset / units_per_cm,
                         "distance_from_waist_cm": waist_offset / units_per_cm}
                       if is_back else
                       {"distance_from_center_front_cm": center_offset / units_per_cm,
                        "distance_from_hem_cm": waist_offset / units_per_cm}),
                    "surface_offset_cm": surface_offset_cm,
                    "surface_stitch_count": len(sewn_edges),
                }
            ],
        )

    def render_svg(self, which: str = "current") -> str:
        spec = self.original_spec if which == "original" else self.current_spec
        if spec is None:
            return ""
        tmp_json = self.work_dir / f"semantic_{which}_{time.time()}_specification.json"
        tmp_svg = self.work_dir / f"semantic_{which}_{time.time()}.svg"
        with tmp_json.open("w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2)
        self._last_render_files.extend([tmp_json, tmp_svg])
        pattern = VisPattern(str(tmp_json))
        dwg = pattern.get_svg(
            str(tmp_svg),
            with_text=False,
            view_ids=False,
            flat=True,
            fill_panels=True,
            margin=2,
        )
        dwg.save()
        return tmp_svg.read_text(encoding="utf-8")

    def save_current_json(self) -> Path:
        if self.current_spec is None:
            raise SemanticEditError("No edited semantic pattern is available")
        out_path = self.work_dir / f"edited_pattern_{time.time()}_specification.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(self.current_spec, f, indent=2, ensure_ascii=False)
        return out_path

    def run_simulation(self) -> SimulationResult:
        if self.current_spec is None:
            raise SemanticEditError("Upload and edit a semantic pattern before simulation")

        return run_pattern_simulation(
            self.current_spec,
            Path.cwd() / "tmp_gui" / "semantic_sim",
            "semantic_edited",
        )


def run_pattern_simulation(spec: SpecDict, sim_root: Path, garment_name: str) -> SimulationResult:
    sim_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{garment_name}_", dir=sim_root) as temp_dir:
        temp_path = Path(temp_dir)
        spec_path = temp_path / f"{garment_name}_specification.json"
        with spec_path.open("w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)

        try:
            props = data_config.Properties("./assets/Sim_props/gui_sim_props.yaml")
            props.set_section_stats(
                "sim",
                fails={},
                sim_time={},
                spf={},
                fin_frame={},
                body_collisions={},
                self_collisions={},
            )
            props.set_section_stats("render", render_time={})

            out_path = sim_root / f"out_{time.time()}"
            paths = PathCofig(
                in_element_path=temp_path,
                out_path=out_path,
                in_name=garment_name,
                out_name=f"{garment_name}_3D",
                body_name="mean_all",
                smpl_body=False,
                add_timestamp=False,
            )

            garment_box_mesh = BoxMesh(paths.in_g_spec, props["sim"]["config"]["resolution_scale"])
            garment_box_mesh.load()
            garment_box_mesh.serialize(
                paths,
                store_panels=False,
                uv_config=props["render"]["config"]["uv_texture"],
            )
            run_sim(
                garment_box_mesh.name,
                props,
                paths,
                save_v_norms=False,
                store_usd=False,
                optimize_storage=False,
                verbose=False,
            )
            sim_mesh_path = paths.g_sim if paths.g_sim.exists() else paths.g_sim_compressed
            if not sim_mesh_path.exists():
                sim_mesh_path = None
            glb_path = None
            glb_traceback = None
            try:
                if sim_mesh_path:
                    mesh = trimesh.load_mesh(sim_mesh_path, force="mesh")
                    try:
                        pbr_material = mesh.visual.material.to_pbr()
                        pbr_material.doubleSided = True
                        mesh.visual.material = pbr_material
                    except BaseException:
                        pass
                    mesh.export(paths.g_sim_glb)
                    if paths.g_sim_glb.exists():
                        glb_path = paths.g_sim_glb
            except BaseException:
                glb_traceback = traceback.format_exc()

            return SimulationResult(
                success=True,
                message=(
                    "Simulation completed"
                    if glb_path
                    else "Simulation completed, but no displayable GLB was generated."
                ),
                out_dir=paths.out_el,
                sim_mesh_path=sim_mesh_path,
                glb_path=glb_path,
                render_front_path=paths.render_path("front") if paths.render_path("front").exists() else None,
                render_back_path=paths.render_path("back") if paths.render_path("back").exists() else None,
                traceback_text=glb_traceback,
            )
        except BaseException as exc:
            return SimulationResult(
                success=False,
                message=str(exc) or exc.__class__.__name__,
                traceback_text=traceback.format_exc(),
            )